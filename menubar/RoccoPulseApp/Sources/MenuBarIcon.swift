import SwiftUI
import RoccoPulseCore

/// The mark rendered next to the menu-bar item. Drives the custom
/// `RoccoMark` view: a chip-die silhouette with a tier-tinted pulse dot
/// that animates when the agent is fresh and the SSH probe succeeded.
///
/// Color choices:
///   .fresh        → body=.primary,   dot=tier color (green/yellow/etc.),
///                   animated
///   .stale        → body=.secondary, dot=.systemYellow
///   .veryStale    → body=.secondary, dot=.systemRed
///   .unreachable  → body=.secondary, dot hidden (a small "no signal" cue)
///   .unknown      → body=.secondary, dot hidden, IconState yields neutral
struct MenuBarIcon: View {
    @ObservedObject var store: StatusStore

    var body: some View {
        let state = IconState.derive(
            snapshot: store.snapshot,
            lastError: store.lastError,
            now: Date()
        )
        RoccoMark(
            bodyTint: bodyColor(for: state),
            pulseTint: dotColor(for: state),
            pulseDotActive: state == .fresh,
            pulseDotHidden: state == .unreachable || state == .unknown
        )
    }

    private func bodyColor(for state: IconState) -> Color {
        switch state {
        case .fresh:                 return .primary
        case .stale, .veryStale,
             .unreachable, .unknown: return .secondary
        }
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
