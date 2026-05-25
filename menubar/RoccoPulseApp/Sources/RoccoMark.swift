import SwiftUI
import AppKit

/// Branded rocco-pulse menubar mark — gradient lightning bolt asset
/// (Assets.xcassets/RoccoMark.imageset) with a tier-tinted pulse dot
/// overlaid in the upper-right corner.
///
/// Why a raster asset and not SwiftUI Canvas/Path:
///   - macOS 14/15 MenuBarExtra reserves the slot for Canvas-rooted labels
///     but never paints them. Image-rooted views render reliably.
///   - We want the gradient + glow look the user asked for; that needs a
///     full-color PNG (Assets.xcassets with template-rendering-intent
///     "original" so macOS does NOT auto-recolor the bolt).
///
/// The asset itself is at:
///   assets/brand/rocco-mark.svg          (source of truth, hand-edited)
///   assets/brand/rocco-mark-{22,44,66}.png  (rasterized via rsvg-convert)
struct RoccoMark: View {
    /// Color of the pulse dot. nil → no dot tint (uses default green).
    var pulseTint: Color? = nil
    /// When true the dot fades in/out gently (used in .fresh state).
    var pulseDotActive: Bool = false
    /// When true the dot is omitted entirely (unreachable / unknown).
    var pulseDotHidden: Bool = false
    /// Subtle grayscale + opacity dim when the agent is stale/unreachable
    /// — lets the user spot a stale read at a glance without losing the
    /// brand silhouette.
    var dimmed: Bool = false

    @State private var pulsing = false

    var body: some View {
        Image("RoccoMark")
            .renderingMode(.original)
            .resizable()
            .scaledToFit()
            .frame(width: 20, height: 20)
            .saturation(dimmed ? 0.25 : 1.0)
            .opacity(dimmed ? 0.55 : 1.0)
            .overlay(alignment: .topTrailing) {
                if !pulseDotHidden {
                    Circle()
                        .fill(pulseTint ?? Color(nsColor: .systemGreen))
                        .frame(width: 6, height: 6)
                        .shadow(color: pulseTint ?? Color(nsColor: .systemGreen),
                                radius: 2)
                        .offset(x: 2, y: -1)
                        .opacity(pulsing && pulseDotActive ? 0.45 : 1.0)
                        .onAppear {
                            guard pulseDotActive else { return }
                            withAnimation(.easeInOut(duration: 1.1)
                                .repeatForever(autoreverses: true)) {
                                pulsing = true
                            }
                        }
                }
            }
            .accessibilityLabel("Rocco Pulse status")
    }
}

#Preview {
    HStack(spacing: 16) {
        RoccoMark(pulseTint: .green, pulseDotActive: true)
        RoccoMark(pulseTint: .yellow,            dimmed: true)
        RoccoMark(pulseTint: .red,               dimmed: true)
        RoccoMark(pulseDotHidden: true,          dimmed: true)
    }
    .padding(40)
    .background(Color.black)
}
