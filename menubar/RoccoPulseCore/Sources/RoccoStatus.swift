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
        public let port: Int
        public let pid: Int?
        public let uptimeS: Int

        public init(running: Bool, model: String?, port: Int, pid: Int?, uptimeS: Int) {
            self.running = running
            self.model = model
            self.port = port
            self.pid = pid
            self.uptimeS = uptimeS
        }

        enum CodingKeys: String, CodingKey {
            case running, model, port, pid
            case uptimeS = "uptime_s"
        }
    }

    public struct Service: Codable, Equatable, Sendable {
        public let port: Int
        public let proc: String
        public let pid: Int
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
    case unreachable
    case unknown

    public static func derive(snapshot: RoccoStatus?, lastError: String?, now: Date) -> IconState {
        if let snapshot {
            let age = now.timeIntervalSince1970 - TimeInterval(snapshot.ts)
            if age <= 60 { return .fresh }
            if age <= 600 { return .stale }
            return .veryStale
        }
        if lastError != nil { return .unreachable }
        return .unknown
    }
}
