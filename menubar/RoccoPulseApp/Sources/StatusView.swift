import SwiftUI
import RoccoPulseCore

struct StatusView: View {
    @EnvironmentObject var store: StatusStore
    @State private var isPerformingLifecycle = false
    @State private var lifecycleNotice: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            // Diagnosis takes precedence over a stale cached snapshot when
            // the most recent poll failed. Otherwise users who ever had a
            // successful poll would see weeks-old GPU data and never the
            // install hint, because loadCachedSnapshot keeps `snapshot`
            // populated across launches. Always-fresh-snapshot wins; cached
            // snapshot loses to an actionable failure.
            let snapshotIsFresh: Bool = {
                guard let snap = store.snapshot else { return false }
                return !snap.isStale(now: Date())
            }()
            if let snapshot = store.snapshot, store.lastError == nil || snapshotIsFresh {
                tierBadge(snapshot: snapshot)
                vllmRow(snapshot: snapshot)
                if snapshot.gpus.isEmpty {
                    Text("No GPUs visible").foregroundStyle(.secondary)
                } else {
                    ForEach(snapshot.gpus, id: \.idx) { gpu in
                        gpuRow(gpu)
                    }
                }
                // Surface "we have data but the latest poll failed" as a
                // small banner under the snapshot — never hide the failure.
                if let err = store.lastError, !snapshotIsFresh {
                    Divider()
                    diagnosis(error: err, kind: store.lastErrorKind)
                }
            } else if let error = store.lastError {
                diagnosis(error: error, kind: store.lastErrorKind)
            } else {
                Text("Waiting for first poll…").foregroundStyle(.secondary)
            }
            Divider()
            footer
        }
        .padding(14)
        .frame(width: 380)
    }

    private var header: some View {
        HStack {
            Text("Rocco Pulse").font(.headline)
            Spacer()
            if let lastFetched = store.lastFetchedAt {
                Text(lastFetched, style: .relative)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func tierBadge(snapshot: RoccoStatus) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(Color(nsColor: TierPalette.color(for: snapshot.tier)))
                .frame(width: 10, height: 10)
            Text("Tier \(snapshot.tier) — \(snapshot.tierReason)")
                .font(.subheadline)
        }
    }

    private func vllmRow(snapshot: RoccoStatus) -> some View {
        HStack {
            Image(systemName: snapshot.vllm.running ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(snapshot.vllm.running ? .green : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(snapshot.vllm.running ? "vLLM up" : "vLLM offline")
                    .font(.subheadline)
                if let model = snapshot.vllm.model {
                    Text(model).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            Button(snapshot.vllm.running ? "Stop" : "Start") {
                Task { await runLifecycle(start: !snapshot.vllm.running) }
            }
            .disabled(isPerformingLifecycle)
        }
    }

    private func gpuRow(_ gpu: RoccoStatus.GPU) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text("GPU \(gpu.idx)").font(.caption).bold()
                Spacer()
                Text("\(gpu.utilPct)% util · \(Int(gpu.memPctUsed))% mem · \(gpu.tempC)°C")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: Double(gpu.utilPct) / 100.0)
                .progressViewStyle(.linear)
        }
    }

    private var footer: some View {
        HStack {
            Button("Refresh") { Task { await store.refresh() } }
            Spacer()
            Menu("Poll: \(store.pollInterval.label)") {
                ForEach(PollInterval.allCases) { interval in
                    Button(interval.label) { store.pollInterval = interval }
                }
            }
            Button("Quit") { NSApp.terminate(nil) }
        }
        .font(.caption)
        .overlay(alignment: .bottom) {
            if let notice = lifecycleNotice {
                Text(notice).font(.caption2).foregroundStyle(.secondary)
                    .padding(.top, 4)
            }
        }
    }

    @MainActor
    private func runLifecycle(start: Bool) async {
        isPerformingLifecycle = true
        defer { isPerformingLifecycle = false }
        let commands = LifecycleCommands()
        do {
            try await Task.detached(priority: .utility) {
                if start { try commands.startVLLM() } else { try commands.stopVLLM() }
            }.value
            lifecycleNotice = start ? "vLLM start requested" : "vLLM stop requested"
            await store.refresh()
        } catch {
            lifecycleNotice = error.localizedDescription
        }
    }

    // MARK: - Failure diagnosis

    /// Branches the empty-snapshot message on what specifically failed.
    /// The biggest win is distinguishing `.agentFileMissing` (SSH is FINE,
    /// the remote daemon just isn't installed) from `.sshFailed` (real
    /// connectivity / auth problem). Previously both rendered as "Could
    /// not reach Rocco" — actively misleading when SSH from Terminal works.
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
                // Surface the captured remote stderr verbatim so the user
                // can spot path / permission mismatches that install.sh
                // wouldn't fix (XDG_CACHE_HOME redirect, wrong user, NFS
                // handle, etc.). Don't make them guess.
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
            // Failure path we don't have a specific hint for — show the raw
            // error and DO NOT pretend it's an SSH auth problem (the old
            // "try ssh-add" hint actively misled people when the actual
            // cause was e.g. a ProcessLauncher timeout or sandbox denial).
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
