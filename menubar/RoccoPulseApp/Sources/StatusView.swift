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
    // Lifecycle state (isPerformingLifecycle, lifecycleNotice,
    // LifecycleNotice) was deleted in the de-dupe pass — the Services
    // section now owns vLLM Start/Stop and has its own self-dismissing
    // toast. One place to flash success/error; one mental model.

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
                // vLLM no longer rendered here — it lives in the Services
                // section below as a single canonical row with its
                // Start/Stop affordance. Showing it both places was
                // strict duplication and the user called it out.
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

    // vllmRow / buttonLabel removed: vLLM now lives ONLY in the Services
    // section at the bottom. The previous "show it twice" layout was
    // strict duplication and the user called it out. `runLifecycle` is
    // still imported by the Services prober via ServiceCommandRunner —
    // those affordances flow through the registry now.

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
            // Unified usage bar: util on top half, mem on bottom half,
            // ZERO gap between them so the two bands read as ONE
            // continuous strip. Previous design had a 2pt gap that the
            // user (rightly) called out as visual noise — at 4pt-each
            // band heights the gap was as tall as the data itself.
            UnifiedUsageBar(
                utilPct: Double(gpu.utilPct) / 100.0,
                memPct: gpu.memPctUsed / 100.0
            )
        }
    }

    /// 8pt-tall capsule track split horizontally into two flush 4pt
    /// bands — top = util (blue), bottom = mem (purple). One Shape,
    /// one track, no inter-band whitespace. Clamps to [0,1].
    private struct UnifiedUsageBar: View {
        let utilPct: Double
        let memPct: Double

        var body: some View {
            GeometryReader { geo in
                let w = geo.size.width
                let h = geo.size.height
                let halfH = h / 2
                let clampedUtil = max(0, min(utilPct, 1))
                let clampedMem  = max(0, min(memPct, 1))
                ZStack(alignment: .topLeading) {
                    // single continuous track — capsule on the outside
                    Capsule(style: .continuous)
                        .fill(Color.primary.opacity(0.10))
                    // top band — util fill (rectangle, no inner capsule
                    // so it sits flush against the bottom band)
                    Rectangle()
                        .fill(Color.blue)
                        .frame(width: w * clampedUtil, height: halfH)
                        .position(x: w * clampedUtil / 2, y: halfH / 2)
                    // bottom band — mem fill, flush against util above
                    Rectangle()
                        .fill(Color.purple)
                        .frame(width: w * clampedMem, height: halfH)
                        .position(x: w * clampedMem / 2, y: halfH + halfH / 2)
                }
                .clipShape(Capsule(style: .continuous))
            }
            .frame(height: 8)
            .accessibilityLabel("GPU usage")
            .accessibilityValue(
                "util \(Int(max(0, min(utilPct, 1)) * 100)) percent, " +
                "memory \(Int(max(0, min(memPct, 1)) * 100)) percent")
        }
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
