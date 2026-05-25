import Foundation

/// Pulls a ServiceStatus for each Service. Three transport implementations
/// behind the curtain — HTTP, SSH+systemctl, in-memory snapshot read —
/// but the caller gets one uniform shape per row.
///
/// Probes are cached for 5s to keep the popover snappy (re-rendering shouldn't
/// re-fire SSH for every keystroke in the parent app). Network probes run on
/// a background queue; from-status probes are synchronous because they're a
/// dictionary lookup.
public final class ServiceProber: @unchecked Sendable {
    private struct CacheEntry {
        let status: ServiceStatus
        let writtenAt: Date
    }
    private let cacheTTL: TimeInterval
    private var cache: [String: CacheEntry] = [:]
    private let cacheLock = NSLock()
    private let session: URLSession
    private let sshLauncher: ProcessLauncher

    public init(cacheTTL: TimeInterval = 5,
                session: URLSession? = nil,
                sshLauncher: ProcessLauncher = RealProcessLauncher()) {
        self.cacheTTL = cacheTTL
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 1.5
        cfg.timeoutIntervalForResource = 2.0
        self.session = session ?? URLSession(configuration: cfg)
        self.sshLauncher = sshLauncher
    }

    /// Probe one service. Reads the cache when fresh; otherwise re-probes
    /// and writes the result. Always returns — never throws — because the
    /// row needs SOMETHING to render and "unknown" is a legitimate state.
    public func probe(_ service: Service, snapshot: RoccoStatus?) async -> ServiceStatus {
        if let hit = cached(service.id) { return hit }
        let result: ServiceStatus
        switch service.kind {
        case .http(let url, let summaryKey):
            result = await probeHTTP(url: url, summaryKey: summaryKey)
        case .sshSystemdUser(let host, let unit):
            result = probeSSHSystemd(host: host, unit: unit)
        case .fromStatus(let path, let label):
            result = probeFromSnapshot(path: path, label: label, snapshot: snapshot)
        case .discovered(let snapshotID):
            result = probeDiscovered(id: snapshotID, snapshot: snapshot)
        }
        store(service.id, result)
        return result
    }

    /// Clear the cache — call when the operator hits Refresh in the popover.
    public func invalidate() {
        cacheLock.lock(); defer { cacheLock.unlock() }
        cache.removeAll()
    }

    // MARK: - cache

    private func cached(_ id: String) -> ServiceStatus? {
        cacheLock.lock(); defer { cacheLock.unlock() }
        guard let entry = cache[id] else { return nil }
        if Date().timeIntervalSince(entry.writtenAt) > cacheTTL {
            cache.removeValue(forKey: id)
            return nil
        }
        return entry.status
    }
    private func store(_ id: String, _ status: ServiceStatus) {
        cacheLock.lock(); defer { cacheLock.unlock() }
        cache[id] = CacheEntry(status: status, writtenAt: Date())
    }

    // MARK: - HTTP

    private func probeHTTP(url: URL, summaryKey: String?) async -> ServiceStatus {
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await session.data(for: req)
            let http = response as? HTTPURLResponse
            guard let code = http?.statusCode, (200..<300).contains(code) else {
                return ServiceStatus(state: .down,
                                     error: "HTTP \(http?.statusCode ?? 0)")
            }
            var summary: String? = nil
            if let key = summaryKey,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                summary = Self.formatSummary(json[key])
            }
            return ServiceStatus(state: .up, summary: summary)
        } catch {
            return ServiceStatus(state: .down, error: error.localizedDescription)
        }
    }

    private static func formatSummary(_ value: Any?) -> String? {
        switch value {
        case let n as Int:    return "\(n.formatted(.number)) turns"
        case let n as Double: return "\(Int(n).formatted(.number)) turns"
        case let s as String: return s
        default:              return nil
        }
    }

    // MARK: - SSH + systemctl --user

    private func probeSSHSystemd(host: String, unit: String) -> ServiceStatus {
        let args = [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            "-o", "ServerAliveInterval=30",
            host,
            "systemctl", "--user", "is-active", unit,
        ]
        do {
            let result = try sshLauncher.run(
                executable: "/usr/bin/ssh",
                arguments: args,
                timeout: 5)
            let out = result.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
            // is-active prints "active" → exit 0, "inactive"/"failed" → exit 3, etc.
            if result.exitCode == 0 || out == "active" {
                return ServiceStatus(state: .up, summary: out)
            }
            return ServiceStatus(state: .down, summary: out.isEmpty ? nil : out)
        } catch {
            return ServiceStatus(state: .unknown,
                                 error: error.localizedDescription)
        }
    }

    // MARK: - from snapshot

    private func probeFromSnapshot(path: String, label: String,
                                   snapshot: RoccoStatus?) -> ServiceStatus {
        guard let snap = snapshot else {
            return ServiceStatus(state: .unknown, error: "no snapshot")
        }
        // Tiny dotted-path resolver. Supports just the few keys we need.
        switch path {
        case "vllm.running":
            if snap.vllm.running {
                return ServiceStatus(state: .up,
                                     summary: snap.vllm.model ?? label)
            }
            // OFFLINE: name the model that WOULD load — pulled from the
            // model_manager's configured_model. Falls back to the
            // built-in `label` (which the registry sets to a sensible
            // default like "Kimi-Dev-72B") only when the agent's older
            // snapshot didn't surface configured_model.
            let next = snap.vllm.configuredModel ?? label
            return ServiceStatus(state: .down,
                                 summary: "will load \(next)")
        default:
            return ServiceStatus(state: .unknown,
                                 error: "unknown path: \(path)")
        }
    }

    private func probeDiscovered(id: String, snapshot: RoccoStatus?) -> ServiceStatus {
        guard let snap = snapshot,
              let svc = snap.services.first(where: { $0.id == id }) else {
            return ServiceStatus(state: .unknown)
        }
        // A discovered service is "up" if rocco-agent could see it via ss.
        // Surface command + user where available.
        var bits: [String] = ["port \(svc.port)"]
        if let cmd = svc.command, !cmd.isEmpty {
            bits.append(cmd.prefix(40) + (cmd.count > 40 ? "…" : ""))
        }
        if let u = svc.user, !u.isEmpty, u != "root", u != "0" {
            bits.append("by \(u)")
        }
        return ServiceStatus(state: .up, summary: bits.joined(separator: " · "))
    }
}
