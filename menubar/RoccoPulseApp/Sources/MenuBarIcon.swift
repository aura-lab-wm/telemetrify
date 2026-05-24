import SwiftUI
import RoccoPulseCore

/// The system-image label rendered next to the menu-bar item. Derives both
/// the SF Symbol and a `.foregroundStyle` color from `(snapshot, lastError,
/// freshness)` via `IconState` + `TierPalette`.
///
/// v0 uses `waveform.path.ecg` for every state and only varies the color —
/// the SF Symbol can be tuned later without breaking the IconState contract.
struct MenuBarIcon: View {
    @ObservedObject var store: StatusStore

    var body: some View {
        let state = IconState.derive(
            snapshot: store.snapshot,
            lastError: store.lastError,
            now: Date()
        )
        Image(systemName: symbolName(for: state))
            .symbolRenderingMode(.hierarchical)
            .foregroundStyle(Color(nsColor: tintColor(for: state)))
    }

    private func symbolName(for state: IconState) -> String {
        switch state {
        case .fresh, .stale, .veryStale: return "waveform.path.ecg"
        case .unreachable: return "waveform.path.ecg.rectangle"
        case .unknown: return "questionmark.circle"
        }
    }

    private func tintColor(for state: IconState) -> NSColor {
        switch state {
        case .fresh:
            // Tier-tinted only when fresh — stale states deliberately desaturate
            // so the user can spot a stale read at a glance.
            return TierPalette.color(for: store.snapshot?.tier)
        case .stale:
            return .systemYellow
        case .veryStale, .unreachable:
            return .systemRed
        case .unknown:
            return .systemGray
        }
    }
}
