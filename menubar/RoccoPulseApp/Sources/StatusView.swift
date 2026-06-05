import SwiftUI
import RoccoPulseCore

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

    private var footer: some View {
        ViewThatFits(in: .horizontal) {
            footerRow
            VStack(alignment: .leading, spacing: 8) {
                footerRefreshRow
                footerControls
            }
        }
        .font(.caption)
    }

    private var footerRow: some View {
        HStack(spacing: 8) {
            footerRefreshRow
            Spacer(minLength: 4)
            footerControls
        }
    }

    private var footerRefreshRow: some View {
        HStack(spacing: 8) {
            Button {
                Task { await store.refresh() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
                    .labelStyle(.titleAndIcon)
            }
            .controlSize(.small)
        }
    }

    private var footerControls: some View {
        HStack(spacing: 8) {
            Picker("Poll", selection: $store.pollInterval) {
                ForEach(PollInterval.allCases) { interval in
                    Text(interval.label).tag(interval)
                }
            }
            .pickerStyle(.menu)
            .controlSize(.small)
            .frame(minWidth: 128, idealWidth: 150, maxWidth: 170)
            Button("Quit") { NSApp.terminate(nil) }
                .controlSize(.small)
        }
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
