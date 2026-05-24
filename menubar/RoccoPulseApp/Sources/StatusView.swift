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
            if let snapshot = store.snapshot {
                tierBadge(snapshot: snapshot)
                vllmRow(snapshot: snapshot)
                if snapshot.gpus.isEmpty {
                    Text("No GPUs visible").foregroundStyle(.secondary)
                } else {
                    ForEach(snapshot.gpus, id: \.idx) { gpu in
                        gpuRow(gpu)
                    }
                }
            } else if let error = store.lastError {
                Text("Could not reach Rocco").bold()
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            } else {
                Text("Waiting for first poll…").foregroundStyle(.secondary)
            }
            Divider()
            footer
        }
        .padding(14)
        .frame(width: 320)
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
}
