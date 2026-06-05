import SwiftUI
import AIPulseCore

/// Layout convention for the popover:
///
///   • Header                          → app name + last-fetch chip
///   • Tier badge                      → "Tier 4 — 4 GPUs free"
///   • vLLM row                        → status + Start/Stop button
///   • GPU section                     → KPI strip + 2×2 ring-gauge grid
///                                       with memory sparklines
///   • Diagnosis banner (conditional)  → install hint or error w/ stderr
///   • Lifecycle notice (conditional)  → result of last Start/Stop click,
///                                       dismissable, NEVER overlays footer
///   • Footer                          → Refresh · Poll · Quit
///
/// Padding is tight on purpose — the popover is glance-fast, not a window.
struct StatusView: View {
    @EnvironmentObject var store: StatusStore
    @State private var selectedPane: PulsePane = .rocco
    /// Drives the live badge's sonar ring (flipped once in onAppear;
    /// the repeatForever animation does the rest).
    @State private var badgePulse = false
    // Lifecycle state (isPerformingLifecycle, lifecycleNotice,
    // LifecycleNotice) was deleted in the de-dupe pass — the Services
    // section now owns vLLM Start/Stop and has its own self-dismissing
    // toast. One place to flash success/error; one mental model.

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                header
                panePicker
            }
            .padding(.horizontal, 12)
            .padding(.top, 12)
            .padding(.bottom, 10)
            Divider()

            VStack(alignment: .leading, spacing: 8) {
                switch selectedPane {
                case .rocco:
                    roccoPane
                case .local:
                    localPane
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)

            Divider()
            footer
                .padding(12)
        }
        .frame(width: popoverWidth)
    }

    private enum PulsePane: String, CaseIterable, Identifiable {
        case rocco = "RoccoPulse"
        case local = "Local-Pulse"
        var id: Self { self }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            Text("AI-Pulse")
                .font(.headline)
            Spacer()
            if let lastFetched = store.lastFetchedAt {
                Text(friendlyAgo(lastFetched))
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .help("Last successful poll")
            }
        }
    }

    private var panePicker: some View {
        HStack(spacing: 4) {
            ForEach(PulsePane.allCases) { pane in
                Button {
                    selectedPane = pane
                } label: {
                    Text(pane.rawValue)
                        .font(.subheadline.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .frame(height: 26)
                        .minimumScaleFactor(0.82)
                }
                .buttonStyle(.plain)
                .foregroundStyle(selectedPane == pane ? .white : .secondary)
                .background(
                    RoundedRectangle(cornerRadius: 7, style: .continuous)
                        .fill(selectedPane == pane
                              ? Color.accentColor
                              : Color.primary.opacity(0.09))
                )
            }
        }
        .frame(height: 30)
    }

    private var popoverWidth: CGFloat {
        let visibleWidth = NSScreen.main?.visibleFrame.width ?? 1440
        return min(460, max(340, visibleWidth * 0.32))
    }

    private var roccoPane: some View {
        VStack(alignment: .leading, spacing: 8) {
            let snapshotIsFresh: Bool = {
                guard let snap = store.snapshot else { return false }
                return !snap.isStale(now: Date())
            }()

            if let snapshot = store.snapshot {
                tierBadge(snapshot: snapshot)
                if snapshot.gpus.isEmpty {
                    Text("No GPUs visible")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    GPUGridSection(gpus: snapshot.gpus,
                                   history: store.gpuHistory)
                }

                if let err = store.lastError, !snapshotIsFresh {
                    diagnosis(error: err, kind: store.lastErrorKind)
                }
            } else if let error = store.lastError {
                diagnosis(error: error, kind: store.lastErrorKind)
            } else {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.mini)
                    Text("Waiting for first poll...")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Divider()
            ServicesSection(scope: .rocco).environmentObject(store)
        }
    }

    private var localPane: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Circle()
                    .fill(Color(nsColor: .systemGreen))
                    .frame(width: 10, height: 10)
                Text("Local-Pulse")
                    .font(.body.bold())
                Text("·").foregroundStyle(.tertiary)
                Text("this Mac")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            ServicesSection(scope: .local).environmentObject(store)
        }
    }

    /// "just now" / "5s ago" / "2m ago" / "1h ago" — the SwiftUI
    /// `.relative` style produces "1 sec, 0 ms" which reads awkwardly in a
    /// menubar chip. This is our own tighter formatter.
    private func friendlyAgo(_ ts: Date) -> String {
        let s = max(0, Int(-ts.timeIntervalSinceNow))
        if s < 5            { return "just now" }
        if s < 60           { return "\(s)s ago" }
        let m = s / 60
        if m < 60           { return "\(m)m ago" }
        let h = m / 60
        if h < 24           { return "\(h)h ago" }
        return "\(h / 24)d ago"
    }

    // MARK: - Tier / vLLM rows

    private func tierBadge(snapshot: RoccoStatus) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(Color(nsColor: TierPalette.color(for: snapshot.tier)))
                .frame(width: 10, height: 10)
            Text("Tier \(snapshot.tier)")
                .font(.body.bold())
            Text("·").foregroundStyle(.tertiary)
            // Model names compress via ModelChip ("WRN-2-70B", not the
            // full HF id) so the reason fits without tail-truncating.
            Text(ModelChip.compressModelNames(in: snapshot.tierReason))
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }

    // vllmRow / buttonLabel removed: vLLM now lives ONLY in the Services
    // section at the bottom. The previous "show it twice" layout was
    // strict duplication and the user called it out. `runLifecycle` is
    // still imported by the Services prober via ServiceCommandRunner —
    // those affordances flow through the registry now.

    // MARK: - Footer

    /// Three evenly-spaced elements, one fixed row: Refresh · live badge ·
    /// Quit. Ghost buttons (quiet until hovered), a sonar-pulsing LIVE
    /// pill with a soft glow, and a 360° spin on Refresh — motion only
    /// where it MEANS something (data flowing, refresh firing).
    private var footer: some View {
        HStack(spacing: 12) {
            FooterGhostButton(symbol: "arrow.clockwise", title: "Refresh",
                              spinsOnTap: true) {
                Task { await store.refresh() }
            }
            Spacer(minLength: 8)
            liveBadge
            Spacer(minLength: 8)
            FooterGhostButton(symbol: "power", title: "Quit",
                              hoverTint: .red) {
                NSApp.terminate(nil)
            }
        }
        .font(.caption)
    }

    /// Stream health at a glance: sonar-pulsing green "LIVE · 2s" while
    /// push frames are flowing, static orange "POLLING · 15s" when the
    /// watchdog has taken over. The pulse ring + glow only render in the
    /// live state — orange stays still so trouble reads as "stopped".
    private var liveBadge: some View {
        let live = store.isLive()
        let tint: Color = live ? .green : .orange
        return HStack(spacing: 6) {
            ZStack {
                if live {
                    Circle()
                        .stroke(tint.opacity(0.6), lineWidth: 1.5)
                        .frame(width: 7, height: 7)
                        .scaleEffect(badgePulse ? 2.1 : 1.0)
                        .opacity(badgePulse ? 0 : 0.9)
                        .animation(.easeOut(duration: 1.4)
                            .repeatForever(autoreverses: false),
                            value: badgePulse)
                }
                Circle()
                    .fill(tint)
                    .frame(width: 7, height: 7)
                    .shadow(color: live ? tint.opacity(0.8) : .clear, radius: 3)
            }
            .frame(width: 16, height: 16)
            Text(live ? "LIVE" : "POLLING")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(1.4)
                .foregroundStyle(tint)
            Text(live ? "2s" : "15s")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .background(Capsule().fill(tint.opacity(0.10)))
        .overlay(Capsule().strokeBorder(tint.opacity(0.25), lineWidth: 1))
        .shadow(color: live ? tint.opacity(0.25) : .clear, radius: 7)
        .onAppear { badgePulse = true }
        .help(live
              ? "Receiving pushed snapshots over the persistent SSH stream"
              : "Stream down — falling back to 15s polling while it reconnects")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(live ? "Live stream connected" : "Polling fallback")
    }

    // MARK: - Failure diagnosis

    @ViewBuilder
    private func diagnosis(error: String, kind: SSHProbeErrorKind?) -> some View {
        switch kind {
        case .agentFileMissing:
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.seal.fill")
                        .foregroundStyle(.green)
                    Text("SSH OK").bold()
                    Text("·").foregroundStyle(.secondary)
                    Text("rocco-agent not installed")
                        .foregroundStyle(.secondary)
                }
                Text("The remote daemon hasn't written `~/.cache/rocco-status.json` yet. Install it from your Mac with:")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("bash menubar/rocco-agent/install.sh rocco")
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Color.gray.opacity(0.18))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
                Text(error)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .textSelection(.enabled)
            }

        case .sshFailed:
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                    Text("SSH to rocco failed").bold()
                }
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                Text("Try: `ssh rocco echo ok` in Terminal. If keys are locked, run `ssh-add`.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

        case .decodeFailed:
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text("Status file malformed").bold()
                }
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                Text("Try restarting the agent: `ssh rocco systemctl --user restart rocco-agent`.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

        case .unknown, .none:
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "questionmark.diamond.fill")
                        .foregroundStyle(.orange)
                    Text("Poll failed").bold()
                }
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                    .textSelection(.enabled)
            }
        }
    }
}

/// Quiet-until-hovered footer button: ghost fill that brightens on
/// hover, optional tint shift (Quit goes red — destructive intent reads
/// before the click), and an optional 360° icon spin on tap (Refresh).
private struct FooterGhostButton: View {
    let symbol: String
    let title: String
    var spinsOnTap = false
    var hoverTint: Color = .primary
    let action: () -> Void

    @State private var hovering = false
    @State private var spinDegrees = 0.0

    var body: some View {
        Button {
            if spinsOnTap {
                withAnimation(.easeOut(duration: 0.6)) { spinDegrees += 360 }
            }
            action()
        } label: {
            HStack(spacing: 5) {
                Image(systemName: symbol)
                    .font(.system(size: 11, weight: .semibold))
                    .rotationEffect(.degrees(spinDegrees))
                Text(title)
                    .font(.callout.weight(.medium))
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(Color.primary.opacity(hovering ? 0.14 : 0.06))
            )
            .contentShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
        }
        .buttonStyle(.plain)
        .foregroundStyle(hovering ? AnyShapeStyle(hoverTint) : AnyShapeStyle(.secondary))
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: 0.15), value: hovering)
        .accessibilityLabel(title)
    }
}
