import SwiftUI
import RoccoPulseCore

/// Layout convention for the popover:
///
///   • Header                          → app name + last-fetch chip
///   • Tier badge                      → "Tier 4 — 4 GPUs free"
///   • vLLM row                        → status + Start/Stop button
///   • GPU rows                        → util + mem bars stacked on a track
///                                       so they're visible even at 0%
///   • Diagnosis banner (conditional)  → install hint or error w/ stderr
///   • Lifecycle notice (conditional)  → result of last Start/Stop click,
///                                       dismissable, NEVER overlays footer
///   • Footer                          → Refresh · Poll · Quit
///
/// Padding is tight on purpose — the popover is glance-fast, not a window.
struct StatusView: View {
    @EnvironmentObject var store: StatusStore
    @State private var isPerformingLifecycle = false
    @State private var lifecycleNotice: LifecycleNotice?

    /// Result of the last lifecycle action — kept in its own type so it can
    /// carry severity (success vs failure) for styling.
    struct LifecycleNotice: Equatable {
        var text: String
        var isError: Bool
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()

            // Diagnosis takes precedence over a stale cached snapshot when
            // the most recent poll failed. Otherwise users who ever had a
            // successful poll would see weeks-old GPU data and never the
            // install hint, because loadCachedSnapshot keeps `snapshot`
            // populated across launches.
            let snapshotIsFresh: Bool = {
                guard let snap = store.snapshot else { return false }
                return !snap.isStale(now: Date())
            }()

            if let snapshot = store.snapshot, store.lastError == nil || snapshotIsFresh {
                tierBadge(snapshot: snapshot)
                vllmRow(snapshot: snapshot)
                if snapshot.gpus.isEmpty {
                    Text("No GPUs visible")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    VStack(spacing: 8) {
                        ForEach(snapshot.gpus, id: \.idx) { gpu in
                            gpuRow(gpu)
                        }
                    }
                }
                // "We have data but the latest poll failed" — never hide it.
                if let err = store.lastError, !snapshotIsFresh {
                    Divider()
                    diagnosis(error: err, kind: store.lastErrorKind)
                }
                Divider()
                ServicesSection().environmentObject(store)
            } else if let error = store.lastError {
                diagnosis(error: error, kind: store.lastErrorKind)
            } else {
                Text("Waiting for first poll…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            // Lifecycle banner: its OWN row, dismissable, NEVER overlaps the
            // footer (which is what the previous .overlay(alignment:.bottom)
            // approach did — the failure message bled into the buttons).
            if let notice = lifecycleNotice {
                lifecycleBanner(notice)
            }

            Divider()
            footer
        }
        .padding(14)
        .frame(width: 380)
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            Text("Rocco Pulse")
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
                .font(.subheadline.bold())
            Text("·").foregroundStyle(.tertiary)
            Text(snapshot.tierReason)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    private func vllmRow(snapshot: RoccoStatus) -> some View {
        HStack(spacing: 10) {
            Image(systemName: snapshot.vllm.running
                  ? "checkmark.circle.fill" : "moon.zzz.fill")
                .foregroundStyle(snapshot.vllm.running ? .green : .secondary)
                .font(.callout)
            VStack(alignment: .leading, spacing: 1) {
                Text(snapshot.vllm.running ? "vLLM up" : "vLLM offline")
                    .font(.subheadline.bold())
                if let model = snapshot.vllm.model {
                    Text(model)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                } else {
                    Text("port \(snapshot.vllm.port) · idle")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer()
            Button(snapshot.vllm.running ? "Stop" : "Start") {
                Task { await runLifecycle(start: !snapshot.vllm.running) }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(isPerformingLifecycle)
        }
    }

    // MARK: - GPU row

    private func gpuRow(_ gpu: RoccoStatus.GPU) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("GPU \(gpu.idx)")
                    .font(.caption.bold())
                Text(gpu.name)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                Text("\(gpu.utilPct)% util · \(Int(gpu.memPctUsed))% mem · \(gpu.tempC)°C")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            // Two-band track: util on top, memory below. The TRACK is always
            // drawn so the lane is visible even when the bar would be 0%.
            // Previously the rows looked empty at idle because ProgressView
            // collapsed entirely.
            VStack(spacing: 2) {
                stackBar(value: Double(gpu.utilPct) / 100.0,
                         tint: .blue,
                         label: "util")
                stackBar(value: gpu.memPctUsed / 100.0,
                         tint: .purple,
                         label: "mem")
            }
        }
    }

    /// A 4pt-tall capsule track with a tinted bar inside. Always visible
    /// (track stays drawn at 0%), accessible (label fires VoiceOver), and
    /// clamps to [0, 1] so a misbehaving agent emitting >100% can't break
    /// the layout.
    private func stackBar(value: Double, tint: Color, label: String) -> some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule(style: .continuous)
                    .fill(Color.primary.opacity(0.10))
                Capsule(style: .continuous)
                    .fill(tint)
                    .frame(width: geo.size.width * max(0, min(value, 1)))
            }
        }
        .frame(height: 4)
        .accessibilityLabel(label)
        .accessibilityValue("\(Int(max(0, min(value, 1)) * 100)) percent")
    }

    // MARK: - Lifecycle banner

    private func lifecycleBanner(_ notice: LifecycleNotice) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: notice.isError
                  ? "exclamationmark.triangle.fill"
                  : "checkmark.circle.fill")
                .foregroundStyle(notice.isError ? .red : .green)
                .font(.caption)
                .padding(.top, 1)
            Text(notice.text)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(3)
            Spacer(minLength: 4)
            Button {
                lifecycleNotice = nil
            } label: {
                Image(systemName: "xmark")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss notice")
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill((notice.isError ? Color.red : Color.green).opacity(0.10))
        )
    }

    // MARK: - Footer

    private var footer: some View {
        HStack(spacing: 8) {
            Button {
                Task { await store.refresh() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
                    .labelStyle(.titleAndIcon)
            }
            .controlSize(.small)
            Spacer(minLength: 4)
            Picker("Poll", selection: $store.pollInterval) {
                ForEach(PollInterval.allCases) { interval in
                    Text(interval.label).tag(interval)
                }
            }
            .pickerStyle(.menu)
            .controlSize(.small)
            .frame(width: 160)
            Button("Quit") { NSApp.terminate(nil) }
                .controlSize(.small)
        }
        .font(.caption)
    }

    // MARK: - Lifecycle action

    @MainActor
    private func runLifecycle(start: Bool) async {
        isPerformingLifecycle = true
        defer { isPerformingLifecycle = false }
        let commands = LifecycleCommands()
        do {
            try await Task.detached(priority: .utility) {
                if start { try commands.startVLLM() } else { try commands.stopVLLM() }
            }.value
            lifecycleNotice = LifecycleNotice(
                text: start ? "vLLM start requested." : "vLLM stop requested.",
                isError: false)
            await store.refresh()
        } catch {
            lifecycleNotice = LifecycleNotice(
                text: error.localizedDescription,
                isError: true)
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
