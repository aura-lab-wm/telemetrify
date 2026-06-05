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
        HStack(spacing: 4) {
            mark(state: state, vllmUp: vllmUp)
            // Dynamic data next to the mark: serving model + avg util
            // ("WRN-2-70B 97%"), bare util when idle, nothing when the
            // snapshot can't be trusted. Logic lives in Core (tested).
            if let title = MenuBarTitle.make(snapshot: store.snapshot, now: Date()) {
                Text(title)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
            }
        }
    }

    @ViewBuilder
    private func mark(state: IconState, vllmUp: Bool) -> some View {
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
