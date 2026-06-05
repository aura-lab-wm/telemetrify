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
    public enum Scope: Equatable, Sendable {
        case rocco
        case local
    }

    public struct LogFile: Equatable, Identifiable, Sendable {
        public enum Location: Equatable, Sendable {
            case local(path: String)
            case remote(host: String, path: String)
        }

        public let id: String
        public let label: String
        public let location: Location

        public init(id: String, label: String, location: Location) {
            self.id = id
            self.label = label
            self.location = location
        }
    }

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
    public let scope: Scope
    public let logFiles: [LogFile]
    /// State-conditional buttons. The view picks the FIRST action whose
    /// `showWhen` matches the live state — single button per row keeps
    /// the popover scannable. Add an action here when a new recovery
    /// path becomes available (e.g. `.restartVLLM`, `.killProcess`).
    public let actions: [ServiceAction]

    public init(id: String, displayName: String, kind: Kind,
                clientURL: URL? = nil, iconSymbol: String = "circle.dotted",
                scope: Scope = .rocco,
                logFiles: [LogFile] = [],
                actions: [ServiceAction] = []) {
        self.id = id
        self.displayName = displayName
        self.kind = kind
        self.clientURL = clientURL
        self.iconSymbol = iconSymbol
        self.scope = scope
        self.logFiles = logFiles
        self.actions = actions
    }

    /// Returns the action whose predicate matches the given state.
    /// Caller-side helper so the View doesn't have to know the matching
    /// rule (currently first-match-wins, may grow priorities later).
    public func action(for state: ServiceStatus.State) -> ServiceAction? {
        actions.first { $0.applies(to: state) }
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
                iconSymbol: "bolt.horizontal.circle",
                scope: .local,
                logFiles: [
                    Service.LogFile(
                        id: "telemetrify-stdout",
                        label: "stdout",
                        location: .local(path: "/Users/amastro/Projects/telemetrify/data/ui-stdout.log")),
                    Service.LogFile(
                        id: "telemetrify-stderr",
                        label: "stderr",
                        location: .local(path: "/Users/amastro/Projects/telemetrify/data/ui-stderr.log")),
                ],
                actions: [
                    // Local LaunchAgent lifecycle straight from the menubar
                    // (no terminal). Disjoint states → one button each:
                    //   down/unknown → Start, up → Restart.
                    // "Open" the dashboard moved onto the row NAME (clientURL)
                    // so the single action button is free for lifecycle.
                    ServiceAction(label: "Start",
                                  showWhen: [.down, .unknown],
                                  command: .startLocalAgent(label: "com.amastropaolo.telemetrify")),
                    ServiceAction(label: "Restart",
                                  showWhen: [.up],
                                  command: .restartLocalAgent(label: "com.amastropaolo.telemetrify")),
                ]
            ))
        }

        out.append(Service(
            id: "rocco-agent",
            displayName: "rocco-agent",
            kind: .sshSystemdUser(host: roccoHost,
                                  unit: "rocco-agent.service"),
            clientURL: nil,
            iconSymbol: "server.rack",
            scope: .rocco,
            logFiles: [
                Service.LogFile(
                    id: "rocco-agent-journal",
                    label: "journal",
                    location: .remote(
                        host: roccoHost,
                        path: "journalctl --user -u rocco-agent.service -n 400 --no-pager")),
            ],
            actions: [
                // Only offer Restart when the unit is actually .down —
                // we don't want a tempting "Restart" button next to a
                // healthy service that the operator could fat-finger.
                ServiceAction(label: "Restart",
                              showWhen: [.down],
                              command: .sshRestartUnit(
                                host: roccoHost,
                                unit: "rocco-agent.service")),
            ]
        ))

        out.append(Service(
            id: "vllm",
            displayName: "vllm",
            kind: .fromStatus(path: "vllm.running",
                              label: "Kimi-Dev-72B"),
            clientURL: nil,
            iconSymbol: "cpu",
            scope: .rocco,
            logFiles: [
                Service.LogFile(
                    id: "vllm-log",
                    label: "vLLM",
                    location: .remote(
                        host: roccoHost,
                        path: "tail -n 400 /scratch/amastropaolo/rocco-inference/logs/vllm.log")),
                Service.LogFile(
                    id: "manager-log",
                    label: "manager",
                    location: .remote(
                        host: roccoHost,
                        path: "tail -n 400 /scratch/amastropaolo/rocco-inference/logs/manager.log")),
            ],
            actions: [
                // Conditional pair: down → "Start", up → "Stop". The
                // ServiceRow picks the first action whose showWhen
                // matches the live state, so order matters only when
                // both predicates could fire (they can't here — the
                // states are disjoint).
                ServiceAction(label: "Start",
                              showWhen: [.down],
                              command: .startVLLM),
                ServiceAction(label: "Stop",
                              showWhen: [.up],
                              command: .stopVLLM),
            ]
        ))

        // LOCAL Mac-side Ollama (Ollama.app serving on :11434). Distinct
        // from any remote ollama the rocco-agent discovers — that one
        // still arrives via merging(discovered:). /api/version returns
        // {"version":"0.24.0"} → summaryKey "version" → row reads "0.24.0".
        if let versionURL = URL(string: "http://127.0.0.1:11434/api/version") {
            out.append(Service(
                id: "ollama-local",
                displayName: "ollama (mac)",
                kind: .http(url: versionURL, summaryKey: "version"),
                clientURL: nil,
                iconSymbol: "circle.hexagongrid",
                scope: .local,
                actions: [
                    // Down/unknown → launch Ollama.app. Reuses .openURL:
                    // NSWorkspace.open on an app-bundle file URL launches
                    // it — no new ServiceCommand case needed. No button
                    // when up (same fat-finger rule as rocco-agent).
                    ServiceAction(label: "Start",
                                  showWhen: [.down, .unknown],
                                  command: .openURL(
                                    URL(fileURLWithPath: "/Applications/Ollama.app"))),
                ]
            ))
        }

        return out
    }

    /// Result of merging built-ins with auto-discovered services from
    /// the agent. Two separate lists so the UI can render:
    ///   - `known` → primary scrollable list (vllm, ollama, jupyter, …)
    ///   - `unknown` → collapsed "Unknown ports" disclosure (random
    ///     high-port listeners we couldn't classify — usually other lab
    ///     users' transient processes, not interesting at a glance)
    public struct MergeResult: Sendable {
        public let known: ServiceRegistry
        public let unknown: [RoccoStatus.Service]
    }

    /// Split discovered services into "known kinds we have an icon and a
    /// story for" and "unknown high-port noise". The previous behavior
    /// surfaced everything except a hardcoded skip-list (sshd / dns /
    /// rpcbind / prometheus) — which meant a Rocco with N lab users
    /// running random tooling produced N rows of `port 60611`-style
    /// noise that crowded out the actually-actionable services. Flipped
    /// the policy to known-only on the main list; unknowns go into the
    /// disclosure.
    public func merging(discovered services: [RoccoStatus.Service],
                        host: String = "rocco") -> MergeResult {
        let builtinKinds: Set<String> = ["vllm", "telemetrify"]
        let skipPorts: Set<Int> = [22, 53, 111, 9100]  // boring infra
        let knownKinds: Set<String> = [
            "vllm", "ollama", "jupyter", "telemetrify", "aura-pulse",
        ]
        var merged = self.services
        var unknown: [RoccoStatus.Service] = []
        for svc in services {
            let kindStr = svc.kind ?? "unknown"
            if skipPorts.contains(svc.port) { continue }
            if builtinKinds.contains(kindStr) { continue }
            // Only surface as a primary row if we recognized the kind —
            // otherwise it's a "port 60611 / amastropaolo / python …"
            // mystery; collect for the disclosure instead.
            if knownKinds.contains(kindStr) {
                merged.append(Service(
                    id: "discovered-\(svc.id)",
                    displayName: discoveredDisplayName(svc: svc, host: host),
                    kind: .discovered(snapshotID: svc.id),
                    clientURL: nil,
                    iconSymbol: iconForKind(kindStr),
                    scope: discoveredScope(kindStr)
                ))
            } else {
                unknown.append(svc)
            }
        }
        return MergeResult(
            known: ServiceRegistry(services: merged),
            unknown: unknown
        )
    }

    /// Discovered rows all come from the rocco-agent snapshot, i.e. they
    /// run on the REMOTE host — name it, so "ollama (rocco)" reads
    /// unambiguously next to the local "ollama (mac)" built-in. The
    /// owning user already shows in the row summary ("… by amastropaolo").
    private func discoveredDisplayName(svc: RoccoStatus.Service,
                                       host: String) -> String {
        let kindStr = svc.kind ?? "unknown"
        if kindStr == "unknown" {
            return "port \(svc.port)"
        }
        return "\(kindStr) (\(host))"
    }

    private func iconForKind(_ kind: String) -> String {
        switch kind {
        case "vllm":          return "cpu"
        case "ollama":        return "circle.hexagongrid"
        case "jupyter":       return "book"
        case "telemetrify":   return "bolt.horizontal.circle"
        case "aura-pulse":    return "waveform.path.ecg"
        case "ssh":           return "key"
        case "prometheus":    return "chart.line.uptrend.xyaxis"
        default:              return "circle.dotted"
        }
    }

    private func discoveredScope(_ kind: String) -> Service.Scope {
        switch kind {
        case "telemetrify", "aura-pulse":
            return .local
        default:
            return .rocco
        }
    }
}
