import SwiftUI
import AIPulseCore

/// Drives the branded `AIPulseMark` view. The bolt asset is always rendered
/// in full color; only the pulse-dot overlay + the `dimmed` modifier vary
/// by `IconState`. That keeps the menu-bar identity recognizable across
/// states while still surfacing freshness / tier at a glance.
///
///   .fresh         → full color, tier-tinted dot OR bolt, animated
///                    (bolt iff vLLM is up + state is .fresh — the
///                    "inference rig hot" signal)
///   .stale         → dimmed,     yellow dot, static
///   .veryStale     → dimmed,     red dot, static
///   .agentMissing  → dimmed,     ORANGE dot, ANIMATED — distinct from
///                                .stale so the operator can tell at a
///                                glance that the fix is "run install.sh"
///                                vs "just wait for the next poll"
///   .unreachable   → dimmed,     dot hidden ("no signal" cue)
///   .unknown       → dimmed,     dot hidden
struct MenuBarIcon: View {
    @ObservedObject var store: StatusStore

    var body: some View {
        let state = IconState.derive(
            snapshot: store.snapshot,
            lastError: store.lastError,
            now: Date(),
            errorKind: store.lastErrorKind
        )
        let vllmUp = store.snapshot?.vllm.running == true
        AIPulseMark(
            pulseTint: dotColor(for: state),
            pulseDotActive: state == .fresh || state == .agentMissing,
            // .agentMissing keeps the dot visible (yellow) — SSH is fine,
            // we want to nudge the user toward the install hint, not signal
            // a full outage.
            pulseDotHidden: state == .unreachable || state == .unknown,
            dimmed: state != .fresh,
            // Only show the bolt when EVERYTHING is healthy. A bolt over
            // a stale/red snapshot would be misleading — the user might
            // think vLLM is up when really we just can't reach Rocco.
            vllmRunning: vllmUp && state == .fresh
        )
    }

    private func dotColor(for state: IconState) -> Color {
        switch state {
        case .fresh:
            return Color(nsColor: TierPalette.color(for: store.snapshot?.tier))
        case .stale:        return Color(nsColor: .systemYellow)
        case .veryStale:    return Color(nsColor: .systemRed)
        // Distinct from .stale (which is also yellow) — orange + animation
        // makes "agent missing" actionable at a glance vs "just stale".
        case .agentMissing: return Color(nsColor: .systemOrange)
        case .unreachable:  return Color(nsColor: .systemRed)
        case .unknown:      return Color(nsColor: .systemGray)
        }
    }
}
