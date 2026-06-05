import Foundation

/// Models exposed by the local LiteLLM gateway (`:4000/v1/models`), grouped by
/// backend for the gateway row's picker. Only the canonical `claude-` aliases
/// are surfaced — bare duplicates (`rabbit`, `opus4.8`, …), wildcards
/// (`rocco-*`) and the dated background-call ids are routing-only noise.
public enum GatewayModelGroup: String, CaseIterable, Sendable {
    case rocco = "Rocco GPU"
    case ollama = "Local Ollama"
    case anthropic = "Anthropic"
}

public struct GatewayModel: Equatable, Sendable, Identifiable {
    public let id: String           // exact /v1/models id; written to settings.json
    public let group: GatewayModelGroup
    public let displayName: String  // prefix-stripped for menus

    public init(id: String, group: GatewayModelGroup, displayName: String) {
        self.id = id
        self.group = group
        self.displayName = displayName
    }
}

public enum GatewayModelList {

    public enum ParseError: Error { case malformed }

    private struct Payload: Decodable {
        struct Entry: Decodable { let id: String }
        let data: [Entry]
    }

    /// Friendly Anthropic aliases the gateway exposes (the real claude-* ids
    /// route via the hidden catch-all and are not listed).
    private static let anthropicPickable: Set<String> = [
        "opus4.8", "sonnet", "haiku",
    ]

    public static func parse(_ data: Data) throws -> [GatewayModel] {
        guard let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
            throw ParseError.malformed
        }
        return payload.data.compactMap { entry in
            let id = entry.id
            if id.contains("*") { return nil }
            if id.hasPrefix("claude-rocco-") {
                return GatewayModel(
                    id: id, group: .rocco,
                    displayName: String(id.dropFirst("claude-".count)))
            }
            if anthropicPickable.contains(id) {
                return GatewayModel(id: id, group: .anthropic, displayName: id)
            }
            // Everything else the gateway lists is a bare local-Ollama tag.
            return GatewayModel(id: id, group: .ollama, displayName: id)
        }
    }
}

/// Reads the gateway master key out of `~/.config/rocco/gateway.env` contents.
public enum GatewayEnv {
    public static func masterKey(from contents: String) -> String? {
        for line in contents.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1)
            if parts.count == 2, parts[0] == "GATEWAY_MASTER_KEY" {
                let v = parts[1].trimmingCharacters(in: .whitespaces)
                return v.isEmpty ? nil : v
            }
        }
        return nil
    }
}

/// Read/update the top-level `"model"` key in `~/.claude/settings.json` —
/// the default model for NEW Claude Code sessions. All sibling keys are
/// preserved verbatim (the file also carries statusLine, hooks, …).
public enum ClaudeDefaultModel {

    public enum UpdateError: Error { case notAnObject }

    public static func current(settingsJSON: Data?) -> String? {
        guard let data = settingsJSON,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return obj["model"] as? String
    }

    public static func updated(settingsJSON: Data?, model: String) throws -> Data {
        var obj: [String: Any] = [:]
        if let data = settingsJSON, !data.isEmpty {
            guard let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw UpdateError.notAnObject
            }
            obj = parsed
        }
        obj["model"] = model
        return try JSONSerialization.data(
            withJSONObject: obj, options: [.prettyPrinted, .sortedKeys])
    }
}
