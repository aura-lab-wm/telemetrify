import Foundation

/// What the fixed-width action gutter OUTSIDE a service card renders.
/// The design rule (docs: design-explorations/ai-pulse-design4-final.html)
/// is that every service row is `HStack { card; gutter(width: 44) }` —
/// the lifecycle action never lives inside the card, so the card's
/// internal layout is identical in every state. This enum is the single
/// source of truth for what the gutter shows; the view just switches.
public enum GutterPresentation: Equatable, Sendable {
    /// Icon-only lifecycle button (play/stop/restart/open).
    case action(symbol: String, verb: String, isDestructive: Bool)
    /// An action is in flight — spinner, no button.
    case busy
    /// No action for this state — dashed ghost so the width stays reserved.
    case placeholder

    public static func make(action: ServiceAction?, inFlight: Bool) -> GutterPresentation {
        if inFlight { return .busy }
        guard let action else { return .placeholder }
        switch action.command {
        case .stopVLLM, .stopLocalAgent:
            return .action(symbol: "stop.fill", verb: "Stop", isDestructive: true)
        case .startVLLM, .startLocalAgent:
            return .action(symbol: "play.fill", verb: "Start", isDestructive: false)
        case .sshRestartUnit, .restartLocalAgent:
            return .action(symbol: "arrow.clockwise", verb: "Restart", isDestructive: false)
        case .openURL:
            return .action(symbol: "arrow.up.right.square", verb: "Open", isDestructive: false)
        case .selectModel:
            // Model selection is the in-card dropdown, never a gutter button.
            return .placeholder
        }
    }
}
