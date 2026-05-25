import Foundation

/// Mirror of the JSON document written by `rocco-agent.py` to
/// `~/.cache/rocco-status.json` on the Rocco host. The schema is authored once
/// in the plan and consumed here verbatim.
public struct RoccoStatus: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let host: String
    public let ts: Int
    public let agentUptimeS: Int
    public let gpus: [GPU]
    public let vllm: VLLM
    public let services: [Service]
    public let tier: Int
    public let tierReason: String
    public let inferenceRecent: InferenceRecent?
    public let errors: [String]

    public init(
        schemaVersion: Int,
        host: String,
        ts: Int,
        agentUptimeS: Int,
        gpus: [GPU],
        vllm: VLLM,
        services: [Service],
        tier: Int,
        tierReason: String,
        inferenceRecent: InferenceRecent?,
        errors: [String]
    ) {
        self.schemaVersion = schemaVersion
        self.host = host
        self.ts = ts
        self.agentUptimeS = agentUptimeS
        self.gpus = gpus
        self.vllm = vllm
        self.services = services
        self.tier = tier
        self.tierReason = tierReason
        self.inferenceRecent = inferenceRecent
        self.errors = errors
    }

    public struct GPU: Codable, Equatable, Sendable {
        public let idx: Int
        public let name: String
        public let utilPct: Int
        public let memUsedMib: Int
        public let memTotalMib: Int
        public let tempC: Int
        public let powerW: Double

        public var memPctUsed: Double {
            guard memTotalMib > 0 else { return 0 }
            return (Double(memUsedMib) / Double(memTotalMib)) * 100.0
        }

        enum CodingKeys: String, CodingKey {
            case idx
            case name
            case utilPct = "util_pct"
            case memUsedMib = "mem_used_mib"
            case memTotalMib = "mem_total_mib"
            case tempC = "temp_c"
            case powerW = "power_w"
        }
    }

    public struct VLLM: Codable, Equatable, Sendable {
        public let running: Bool
        public let model: String?
        // `port` is what the agent expects vLLM to listen on; it knows the
        // configured port regardless of whether vLLM is currently up.
        public let port: Int
        public let pid: Int?
        // Optional: when vLLM isn't running the agent emits `uptime_s: null`
        // because there's no process whose uptime to measure. Was previously
        // declared non-optional which caused every real snapshot from the
        // remote to fail Codable with a DecodingError, surfacing as
        // `.decodeFailed` and the misleading "Status file malformed" popover.
        public let uptimeS: Int?
        // Configured model id (e.g. "Kimi-Dev-72B") — populated by the
        // agent ONLY when vLLM is offline. Lets the popover say
        // "will load Kimi-Dev-72B when tier allows" instead of the
        // anemic "port 8000 · idle". The agent reads this from the
        // model_manager's `state.model_id` so the answer matches the
        // tier's `description` shown elsewhere in the popover.
        public let configuredModel: String?

        public init(running: Bool, model: String?, port: Int, pid: Int?,
                    uptimeS: Int?, configuredModel: String? = nil) {
            self.running = running
            self.model = model
            self.port = port
            self.pid = pid
            self.uptimeS = uptimeS
            self.configuredModel = configuredModel
        }

        enum CodingKeys: String, CodingKey {
            case running, model, port, pid
            case uptimeS = "uptime_s"
            case configuredModel = "configured_model"
        }
    }

    public struct Service: Codable, Equatable, Sendable, Identifiable {
        public let port: Int
        public let proc: String
        // Optional: `ss -tlnH` can't always determine the owning PID (e.g.
        // ports owned by another user, kernel pseudo-services, processes
        // whose /proc entry is unreadable) — the agent emits `null` for
        // those. Was previously non-optional which made any real-world
        // snapshot fail Codable.
        public let pid: Int?

        // Schema v2 — all optional so v1 snapshots still decode.
        // `kind` is one of: vllm | ollama | jupyter | telemetrify | ssh |
        // prometheus | nfs-portmap | dns-stub | unknown.
        public let kind: String?
        public let command: String?     // /proc/<pid>/cmdline, NUL→space
        public let user: String?        // login name resolved from Uid:

        // Schema v3 — HTTP banner the agent saw when probing
        // `localhost:port`. Populated only for `kind == "unknown"`.
        // Powers the menubar's "Identify with AI" classifier — gives
        // the LLM enough fingerprint to distinguish ZMQ from gRPC from
        // a Go web API from a Python ipykernel.
        public let probe: String?

        public var id: String { "\(port)-\(proc)-\(pid ?? 0)" }

        public init(port: Int, proc: String, pid: Int?,
                    kind: String? = nil, command: String? = nil,
                    user: String? = nil, probe: String? = nil) {
            self.port = port
            self.proc = proc
            self.pid = pid
            self.kind = kind
            self.command = command
            self.user = user
            self.probe = probe
        }
    }

    public struct InferenceRecent: Codable, Equatable, Sendable {
        public let requestsLast5m: Int
        public let avgLatencyMs: Double

        enum CodingKeys: String, CodingKey {
            case requestsLast5m = "requests_last_5m"
            case avgLatencyMs = "avg_latency_ms"
        }
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case host, ts, gpus, vllm, services, tier, errors
        case agentUptimeS = "agent_uptime_s"
        case tierReason = "tier_reason"
        case inferenceRecent = "inference_recent"
    }

    // MARK: - Decoding helpers

    public static func decode(from data: Data) throws -> RoccoStatus {
        try JSONDecoder().decode(RoccoStatus.self, from: data)
    }

    // MARK: - Freshness

    /// Snapshots older than 60s are considered stale. Anything older than 600s
    /// is "very stale" — see `IconState`.
    public func isStale(now: Date) -> Bool {
        now.timeIntervalSince1970 - TimeInterval(ts) > 60
    }
}

/// Discrete state derived from (snapshot, lastError, freshness). The menubar
/// icon's SF Symbol + foreground color are a function of this enum.
public enum IconState: String, Equatable, Sendable {
    case fresh
    case stale
    case veryStale
    case agentMissing   // SSH OK, but rocco-status.json missing → install hint
    case unreachable    // SSH itself failed (network / auth / host key / …)
    case unknown

    /// Legacy two-argument overload used before the probe started
    /// classifying failures. Defaults `errorKind` to `nil` so a `lastError`
    /// without a typed kind is still classified as `.unreachable` (the
    /// previous behavior). New call-sites should pass `errorKind` so
    /// "agent file missing" can be distinguished from "ssh down".
    public static func derive(snapshot: RoccoStatus?, lastError: String?, now: Date) -> IconState {
        derive(snapshot: snapshot, lastError: lastError, now: now, errorKind: nil)
    }

    public static func derive(
        snapshot: RoccoStatus?,
        lastError: String?,
        now: Date,
        errorKind: SSHProbeErrorKind?
    ) -> IconState {
        if let snapshot {
            let age = now.timeIntervalSince1970 - TimeInterval(snapshot.ts)
            if age <= 60 { return .fresh }
            if age <= 600 { return .stale }
            return .veryStale
        }
        // .agentMissing — the actionable yellow state. Both "agent file
        // missing" AND "JSON malformed" surface here because the icon
        // color must match the popover header color, and both popovers
        // render in orange/yellow. A red `.unreachable` for decodeFailed
        // would lie about the severity.
        switch errorKind {
        case .agentFileMissing, .decodeFailed: return .agentMissing
        case .sshFailed, .unknown, .none:      break
        }
        if lastError != nil { return .unreachable }
        return .unknown
    }
}
