import Foundation
import RoccoPulseCore

/// Talks to telemetrify's `/api/classify-ports` on localhost. Sends the
/// unknown-port snapshot rows, receives LLM-classified labels. Decoupled
/// from the view so the view can stay declarative.
///
/// We default to http://127.0.0.1:8767 (the launchd-managed telemetrify
/// UI) so this works out of the box on this user's setup. Override via
/// `TELEMETRIFY_URL` env var if telemetrify lives elsewhere.
public actor PortClassifierClient {

    public struct Classification: Codable, Equatable, Sendable {
        public let port: Int
        public let kind: String
        public let label: String
        public let confidence: String
        public let reasoning: String
    }

    public struct Response: Codable, Sendable {
        public let classifications: [Classification]
        public let backend: String?
        /// Set when the LLM responded but its output didn't parse as JSON.
        /// The endpoint still returns 200 in that case so the UI can
        /// distinguish "AI gave up" from "telemetrify itself is down".
        public let error: String?
        public let raw_preview: String?
    }

    public enum ClassifyError: LocalizedError {
        case telemetrifyUnreachable(URL)
        case httpError(Int, String)
        case decodeFailed(Error)
        case aiOutputUnparseable(String, rawPreview: String)
        public var errorDescription: String? {
            switch self {
            case .telemetrifyUnreachable(let u):
                return "telemetrify not reachable at \(u.absoluteString) — is the LaunchAgent running?"
            case .httpError(let code, let body):
                return "HTTP \(code): \(body.prefix(200))"
            case .decodeFailed(let e):
                return "decode failed: \(e)"
            case .aiOutputUnparseable(let msg, _):
                return "Couldn't classify ports — \(msg). Try Identify again in a moment."
            }
        }
    }

    private let endpoint: URL
    private let session: URLSession

    public init(endpoint: URL? = nil) {
        if let e = endpoint {
            self.endpoint = e
        } else if let override = ProcessInfo.processInfo.environment["TELEMETRIFY_URL"],
                  let url = URL(string: override.trimmingCharacters(in: .whitespacesAndNewlines)
                                    + "/api/classify-ports") {
            self.endpoint = url
        } else {
            self.endpoint = URL(string: "http://127.0.0.1:8767/api/classify-ports")!
        }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 25
        cfg.timeoutIntervalForResource = 35
        self.session = URLSession(configuration: cfg)
    }

    /// Send the unknown rows to telemetrify and get back classifications.
    /// Returns a port → Classification dict for easy lookup in the view.
    public func classify(_ services: [RoccoStatus.Service]) async throws -> [Int: Classification] {
        // Wire JSON: [{port, proc, command, user, probe}]
        struct Outbound: Encodable {
            let port: Int
            let proc: String?
            let command: String?
            let user: String?
            let probe: String?
        }
        let payload = services.map {
            Outbound(port: $0.port, proc: $0.proc.isEmpty ? nil : $0.proc,
                     command: $0.command, user: $0.user, probe: $0.probe)
        }
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["ports": payload.map(Self.dictify)])

        let (data, resp): (Data, URLResponse)
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw ClassifyError.telemetrifyUnreachable(endpoint)
        }
        guard let http = resp as? HTTPURLResponse,
              (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            throw ClassifyError.httpError(
                (resp as? HTTPURLResponse)?.statusCode ?? -1, body)
        }
        do {
            let decoded = try JSONDecoder().decode(Response.self, from: data)
            // Endpoint returns 200 with an `error` field when the AI
            // couldn't produce parseable JSON. Surface it as a real
            // error so the UI shows the friendly banner instead of
            // silently rendering an empty classifications dict.
            if decoded.classifications.isEmpty, let err = decoded.error {
                throw ClassifyError.aiOutputUnparseable(err,
                    rawPreview: decoded.raw_preview ?? "")
            }
            var out: [Int: Classification] = [:]
            for c in decoded.classifications { out[c.port] = c }
            return out
        } catch let e as ClassifyError {
            throw e
        } catch {
            throw ClassifyError.decodeFailed(error)
        }
    }

    /// Turn an Encodable struct into a dict for the URLRequest body —
    /// JSONSerialization.data needs a dict, not arbitrary Encodable.
    private static func dictify(_ value: Encodable) -> [String: Any] {
        guard let data = try? JSONEncoder().encode(AnyEncodable(value)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        // Drop nil-valued keys to keep the request body tight.
        return obj.compactMapValues { v in v is NSNull ? nil : v }
    }
}

/// Type-erased Encodable so we can JSONEncoder().encode any value.
private struct AnyEncodable: Encodable {
    let value: Encodable
    init(_ value: Encodable) { self.value = value }
    func encode(to encoder: Encoder) throws { try value.encode(to: encoder) }
}
