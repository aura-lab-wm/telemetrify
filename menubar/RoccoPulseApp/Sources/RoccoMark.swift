import SwiftUI

/// Custom menubar mark — `server.rack` SF Symbol with a tier-tinted pulse
/// dot overlaid in the upper-right corner.
///
/// We use a real `Image(systemName:)` as the base because pure-SwiftUI
/// Canvas / Path labels render to empty space inside `MenuBarExtra` on
/// macOS 14–15 (the system reserves the slot but never paints the view).
/// Compositing a Circle overlay on top of an SF Symbol is the most
/// reliable way to keep tier-tinted state without losing visibility.
struct RoccoMark: View {
    var bodyTint: Color? = nil
    var pulseTint: Color? = nil
    var pulseDotActive: Bool = false
    var pulseDotHidden: Bool = false

    @State private var pulsing = false

    var body: some View {
        Image(systemName: "server.rack")
            .symbolRenderingMode(.hierarchical)
            .foregroundStyle(bodyTint ?? .primary)
            .overlay(alignment: .topTrailing) {
                if !pulseDotHidden {
                    Circle()
                        .fill(pulseTint ?? bodyTint ?? .primary)
                        .frame(width: 6, height: 6)
                        .overlay(
                            // soft halo for extra punch in the bar
                            Circle()
                                .stroke(pulseTint ?? bodyTint ?? .primary,
                                        lineWidth: 0.5)
                                .opacity(0.4)
                                .scaleEffect(1.6)
                        )
                        .offset(x: 3, y: -2)
                        .opacity(pulsing && pulseDotActive ? 0.5 : 1.0)
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
    HStack(spacing: 14) {
        RoccoMark(bodyTint: .primary, pulseTint: .green, pulseDotActive: true)
        RoccoMark(bodyTint: .secondary, pulseTint: .yellow)
        RoccoMark(bodyTint: .secondary, pulseTint: .red)
        RoccoMark(bodyTint: .secondary, pulseDotHidden: true)
    }
    .padding(40)
    .background(Color.black)
}
