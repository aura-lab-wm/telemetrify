import SwiftUI
import RoccoPulseCore

/// Drives the branded `RoccoMark` view. The bolt asset is always rendered
/// in full color; only the pulse-dot overlay + the `dimmed` modifier vary
/// by `IconState`. That keeps the menu-bar identity recognizable across
/// states while still surfacing freshness / tier at a glance.
///
///   .fresh        → full color, tier-tinted dot, animated
///   .stale        → dimmed,     yellow dot
///   .veryStale    → dimmed,     red dot
///   .unreachable  → dimmed,     dot hidden ("no signal" cue)
///   .unknown      → dimmed,     dot hidden
struct MenuBarIcon: View {
    @ObservedObject var store: StatusStore

    var body: some View {
        let state = IconState.derive(
            snapshot: store.snapshot,
            lastError: store.lastError,
            now: Date()
        )
        RoccoMark(
            pulseTint: dotColor(for: state),
            pulseDotActive: state == .fresh,
            pulseDotHidden: state == .unreachable || state == .unknown,
            dimmed: state != .fresh
        )
    }

    private func dotColor(for state: IconState) -> Color {
        switch state {
        case .fresh:
            return Color(nsColor: TierPalette.color(for: store.snapshot?.tier))
        case .stale:       return Color(nsColor: .systemYellow)
        case .veryStale:   return Color(nsColor: .systemRed)
        case .unreachable: return Color(nsColor: .systemRed)
        case .unknown:     return Color(nsColor: .systemGray)
        }
    }
}
