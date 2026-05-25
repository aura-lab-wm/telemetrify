import Foundation

/// A "service" is anything the user wants a glance-fast status row for —
/// a Mac-side HTTP endpoint, a Rocco-side systemd-user unit, or a value
/// pulled out of the already-fetched rocco-status.json. The registry is
/// the union of:
///
///   1. Hard-coded built-ins (always present)
///   2. Auto-discovered services from `RoccoStatus.services[]` (whatever
///      the agent's port-scan + classifier found)
///
/// A future iteration can add a `~/.config/rocco-pulse/services.toml`
/// loader; the registry was deliberately shaped to make that additive.
public struct Service: Equatable, Identifiable, Sendable {
    public enum Kind: Equatable, Sendable {
        /// Plain HTTP probe. `summaryKey` (when set) is a top-level key on
        /// the JSON response whose value is rendered as the row's summary
        /// (e.g. telemetrify's `/api/health` returns `{"turns": 12798, …}`
        /// → summaryKey "turns" → row reads "12,798 turns").
        case http(url: URL, summaryKey: String?)
        /// `ssh <host> systemctl --user is-active <unit>`. 1s timeout,
        /// cached for 5s so we don't ssh on every popover open.
        case sshSystemdUser(host: String, unit: String)
        /// Read straight out of the already-fetched RoccoStatus snapshot.
        /// `path` is dotted ("vllm.running"). No extra round-trip.
        case fromStatus(path: String, label: String)
        /// Auto-discovered listening port on Rocco. The row renders the
        /// classifier's `kind` + port + Linux user from the snapshot.
        case discovered(snapshotID: String)
    }

    public let id: String
    public let displayName: String
    public let kind: Kind
    /// Optional URL handled by `open` when the operator hits the row's
    /// primary button ("Open"). nil → no button.
    public let clientURL: URL?
    /// SF Symbol name for the row icon. Pick something visually distinct
    /// from rocco-pulse's brand bolt so service rows don't compete with
    /// the menubar identity.
    public let iconSymbol: String

    public init(id: String, displayName: String, kind: Kind,
                clientURL: URL? = nil, iconSymbol: String = "circle.dotted") {
        self.id = id
        self.displayName = displayName
        self.kind = kind
        self.clientURL = clientURL
        self.iconSymbol = iconSymbol
    }
}

public struct ServiceStatus: Equatable, Sendable {
    public enum State: Equatable, Sendable {
        case up
        case down
        case unknown
    }
    public let state: State
    public let summary: String?   // e.g. "12,798 turns" or "port 8000"
    public let error: String?     // diagnostic if state == .down

    public init(state: State, summary: String? = nil, error: String? = nil) {
        self.state = state
        self.summary = summary
        self.error = error
    }
}

/// Bundle of services to render. The Mac client always shows the built-ins
/// first (predictable layout) then any rows discovered by the agent.
public struct ServiceRegistry: Sendable {
    public let services: [Service]
    public init(services: [Service]) { self.services = services }

    /// Hard-coded defaults. Adjust here when adding a permanent service —
    /// dynamic services come in via `merging(discovered:)`.
    public static func builtins(roccoHost: String = "rocco") -> [Service] {
        var out: [Service] = []

        if let healthURL = URL(string: "http://127.0.0.1:8767/api/health"),
           let webURL = URL(string: "http://127.0.0.1:8767/") {
            out.append(Service(
                id: "telemetrify",
                displayName: "telemetrify",
                kind: .http(url: healthURL, summaryKey: "turns"),
                clientURL: webURL,
                iconSymbol: "bolt.horizontal.circle"
            ))
        }

        out.append(Service(
            id: "rocco-agent",
            displayName: "rocco-agent",
            kind: .sshSystemdUser(host: roccoHost,
                                  unit: "rocco-agent.service"),
            clientURL: nil,
            iconSymbol: "server.rack"
        ))

        out.append(Service(
            id: "vllm",
            displayName: "vllm",
            kind: .fromStatus(path: "vllm.running",
                              label: "Kimi-Dev-72B"),
            clientURL: nil,
            iconSymbol: "cpu"
        ))

        return out
    }

    /// Merge the built-ins with whatever the rocco-agent auto-discovered
    /// in `RoccoStatus.services[]`. Skip discovered rows whose `kind`
    /// already has a hand-coded built-in to avoid double-listing (we don't
    /// want a "vllm (port 8000)" row right next to the built-in "vllm"
    /// from-status row).
    public func merging(discovered services: [RoccoStatus.Service]) -> ServiceRegistry {
        let builtinKinds: Set<String> = ["vllm", "telemetrify"]
        let skipPorts: Set<Int> = [22, 53, 111, 9100]  // boring infra
        var merged = self.services
        for svc in services {
            let kindStr = svc.kind ?? "unknown"
            if builtinKinds.contains(kindStr) { continue }
            if skipPorts.contains(svc.port) { continue }
            merged.append(Service(
                id: "discovered-\(svc.id)",
                displayName: discoveredDisplayName(svc: svc),
                kind: .discovered(snapshotID: svc.id),
                clientURL: nil,
                iconSymbol: iconForKind(kindStr)
            ))
        }
        return ServiceRegistry(services: merged)
    }

    private func discoveredDisplayName(svc: RoccoStatus.Service) -> String {
        let kindStr = svc.kind ?? "unknown"
        if kindStr == "unknown" {
            return "port \(svc.port)"
        }
        if let u = svc.user, !u.isEmpty, u != "0", u != "root" {
            return "\(kindStr) (\(u))"
        }
        return kindStr
    }

    private func iconForKind(_ kind: String) -> String {
        switch kind {
        case "vllm":          return "cpu"
        case "ollama":        return "circle.hexagongrid"
        case "jupyter":       return "book"
        case "telemetrify":   return "bolt.horizontal.circle"
        case "ssh":           return "key"
        case "prometheus":    return "chart.line.uptrend.xyaxis"
        default:              return "circle.dotted"
        }
    }
}
