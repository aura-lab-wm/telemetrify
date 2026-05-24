import AppKit

/// Maps the agent's tier number (1=worst → 5=best) to a `NSColor`. Anything
/// outside 1…5 falls back to gray so a malformed snapshot still renders.
public enum TierPalette {
    public static func color(for tier: Int?) -> NSColor {
        switch tier {
        case 1: return .systemRed
        case 2: return .systemOrange
        case 3: return .systemYellow
        case 4: return .systemGreen
        case 5: return .systemMint
        default: return .systemGray
        }
    }
}
