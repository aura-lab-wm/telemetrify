import Foundation
import AIPulseCore

/// Feeds the gateway row's model picker: the pickable roster from
/// `:4000/v1/models` (master-key auth from gateway.env), the operator's
/// current default model from `~/.claude/settings.json`, and the select
/// action (write default + pre-warm rocco tiers via the shim's /v1/switch).
@MainActor
final class GatewayPickerStore: ObservableObject {

    @Published private(set) var models: [GatewayModel] = []
    @Published private(set) var selectedId: String?

    private let settingsURL = URL(fileURLWithPath: NSHomeDirectory() + "/.claude/settings.json")
    private let envURL = URL(fileURLWithPath: NSHomeDirectory() + "/.config/rocco/gateway.env")
    private let modelsURL = URL(string: "http://127.0.0.1:4000/v1/models?limit=1000")!
    private let switchURL = URL(string: "http://127.0.0.1:4010/v1/switch")!

    func refresh() async {
        selectedId = ClaudeDefaultModel.current(
            settingsJSON: try? Data(contentsOf: settingsURL))
        // The roster only changes when the gateway config does — one fetch
        // per app run is plenty; the popover re-renders from cache instantly.
        guard models.isEmpty else { return }
        guard let env = try? String(contentsOf: envURL, encoding: .utf8),
              let key = GatewayEnv.masterKey(from: env) else { return }
        var req = URLRequest(url: modelsURL, timeoutInterval: 4)
        req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let parsed = try? GatewayModelList.parse(data) else { return }
        models = parsed
    }

    /// Write the default model for new claude sessions; pre-warm the GPU tier
    /// for rocco picks. Returns the toast text — never throws.
    func select(_ m: GatewayModel) async -> (message: String, isError: Bool) {
        do {
            let updated = try ClaudeDefaultModel.updated(
                settingsJSON: try? Data(contentsOf: settingsURL), model: m.id)
            try updated.write(to: settingsURL, options: .atomic)
            selectedId = m.id
        } catch {
            return ("couldn't write ~/.claude/settings.json: \(error.localizedDescription)", true)
        }
        guard m.group == .rocco else {
            return ("\(m.displayName) is now the default for new claude sessions", false)
        }
        var req = URLRequest(url: switchURL, timeoutInterval: 8)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["model": m.id])
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let code = (resp as? HTTPURLResponse)?.statusCode,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return ("\(m.displayName) set as default — shim unreachable, tier not pre-warmed", true)
        }
        let status = obj["status"] as? String ?? "unknown"
        switch (code, status) {
        case (200, _):
            return ("\(m.displayName) set as default — already loaded on rocco", false)
        case (202, "switching"):
            return ("\(m.displayName) set as default — rocco switching (~2–4 min)", false)
        case (202, _):
            return ("\(m.displayName) set as default — rocco loading", false)
        default:
            let msg = ((obj["error"] as? [String: Any])?["message"] as? String) ?? status
            return ("\(m.displayName) set as default — rocco: \(msg)", true)
        }
    }
}
