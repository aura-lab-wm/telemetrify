import SwiftUI
import AppKit

/// Branded AI-Pulse menubar mark — gradient lightning bolt asset
/// (Assets.xcassets/AIPulseMark.imageset) with a tier-tinted pulse dot
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
struct AIPulseMark: View {
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
    /// When true the corner indicator becomes a tiny lightning bolt
    /// instead of a circle — the at-a-glance "vLLM is up and serving"
    /// signal the user asked for ("when VLLMs is running show that in
    /// the menubar with the flash icon"). The bolt is layered on top
    /// of the brand mark so the menu bar reads as "inference rig hot".
    var vllmRunning: Bool = false

    @State private var pulsing = false

    var body: some View {
        Image("AIPulseMark")
            .renderingMode(.original)
            .resizable()
            .scaledToFit()
            .frame(width: 20, height: 20)
            .saturation(dimmed ? 0.25 : 1.0)
            .opacity(dimmed ? 0.55 : 1.0)
            .overlay(alignment: .topTrailing) {
                if !pulseDotHidden {
                    cornerIndicator
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
            .accessibilityLabel(vllmRunning
                ? "AI-Pulse status — vLLM up"
                : "AI-Pulse status")
    }

    /// The dot OR the bolt — picked per `vllmRunning`. The bolt is a
    /// system symbol filled with the same tint as the dot would have
    /// been, so the "green = healthy" colorway carries over verbatim.
    @ViewBuilder
    private var cornerIndicator: some View {
        let tint = pulseTint ?? Color(nsColor: .systemGreen)
        if vllmRunning {
            Image(systemName: "bolt.fill")
                .symbolRenderingMode(.monochrome)
                .font(.system(size: 9, weight: .black))
                .foregroundStyle(tint)
                .shadow(color: tint.opacity(0.9), radius: 2.5)
                .offset(x: 3, y: -2)
        } else {
            Circle()
                .fill(tint)
                .frame(width: 6, height: 6)
                .shadow(color: tint, radius: 2)
                .offset(x: 2, y: -1)
        }
    }
}

#Preview {
    HStack(spacing: 16) {
        AIPulseMark(pulseTint: .green, pulseDotActive: true)
        AIPulseMark(pulseTint: .yellow,            dimmed: true)
        AIPulseMark(pulseTint: .red,               dimmed: true)
        AIPulseMark(pulseDotHidden: true,          dimmed: true)
    }
    .padding(40)
    .background(Color.black)
}
