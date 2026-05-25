import SwiftUI
import RoccoPulseCore

/// Compact "Services" list rendered between the GPU rows and the lifecycle
/// banner. Each row shows: status dot, icon, name, status summary, and the
/// FIRST action whose `showWhen` matches the live state — so a `.down`
/// service shows "Start" / "Restart", a `.up` one shows "Stop", etc. Open
/// always wins for http-kind services because the URL is a valid recovery
/// path regardless of status.
struct ServicesSection: View {
    @EnvironmentObject var store: StatusStore
    @StateObject private var model = ServicesViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("SERVICES")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .tracking(1.0)
                Spacer()
                if model.isBusy {
                    ProgressView().controlSize(.mini)
                }
            }

            // Known services render directly (no ScrollView) — there's
            // only ever a handful: the three built-ins plus whatever
            // ollama / jupyter the agent's classifier found. Wrapping
            // these in a ScrollView made it collapse to 0 height
            // because SwiftUI ScrollView is greedy and the parent
            // VStack doesn't pin a minimum. The Unknown disclosure
            // below DOES need scrolling because it can have dozens
            // of rows.
            VStack(spacing: 4) {
                ForEach(model.rows) { row in
                    ServiceRowView(
                        row: row,
                        inFlight: model.inFlight.contains(row.service.id)
                    ) { action in
                        Task { await model.perform(action: action, on: row.service) }
                    }
                }
                // Empty-state hint while the first poll is still in
                // flight — without it the user sees a SERVICES header
                // with nothing under it and assumes the section is
                // broken (which is exactly the bug they reported).
                if model.rows.isEmpty {
                    Text(model.isBusy
                         ? "checking services…"
                         : "no services yet")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .padding(.vertical, 2)
                }
            }

            // Unknown-ports disclosure — collapsed by default, expand to
            // see the dozens of random high-port listeners the agent
            // surfaced but the classifier couldn't tag. Usually other
            // lab users' transient processes.
            if !model.unknownPorts.isEmpty {
                UnknownPortsDisclosure(ports: model.unknownPorts)
            }

            if let toast = model.toast {
                ServiceToastView(toast: toast) { model.dismissToast() }
            }
        }
        .task(id: store.lastFetchedAt) {
            model.bind(store: store)   // so perform() can trigger refreshes
            await model.refresh(snapshot: store.snapshot)
        }
    }
}

private struct UnknownPortsDisclosure: View {
    let ports: [RoccoStatus.Service]
    @State private var isExpanded: Bool = false
    @State private var classifying: Bool = false
    @State private var classifications: [Int: PortClassifierClient.Classification] = [:]
    @State private var classifyError: String? = nil
    private let client = PortClassifierClient()

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            VStack(alignment: .leading, spacing: 6) {
                // Toolbar: "Identify with AI" button + status
                HStack(spacing: 8) {
                    Button {
                        Task { await runClassification() }
                    } label: {
                        Label(classifying ? "Identifying…" : "Identify with AI",
                              systemImage: "sparkles")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(classifying)
                    if classifying {
                        ProgressView().controlSize(.mini)
                    }
                    Spacer()
                }
                if let err = classifyError {
                    // Soft warning, not a stack-trace. The raw upstream
                    // error (HTTP 502 + Python dict-repr) was too scary.
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.caption)
                            .foregroundStyle(.orange)
                        Text(err)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(3)
                            .textSelection(.enabled)
                    }
                    .padding(8)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(Color.orange.opacity(0.10))
                    )
                }

                ScrollView(.vertical, showsIndicators: true) {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(ports, id: \.id) { svc in
                            unknownRow(svc)
                        }
                    }
                }
                .frame(maxHeight: 140)
            }
            .padding(.top, 4)
        } label: {
            HStack(spacing: 4) {
                Text("Unknown ports")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Text("\(ports.count)")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Color.secondary.opacity(0.18))
                    .clipShape(Capsule())
            }
        }
        .padding(.top, 4)
    }

    @ViewBuilder
    private func unknownRow(_ svc: RoccoStatus.Service) -> some View {
        let cls = classifications[svc.port]
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Circle().fill(cls != nil ? Color.accentColor : Color.secondary)
                .frame(width: 6, height: 6)
            Text("port \(String(svc.port))")
                .font(.system(.footnote, design: .monospaced))
                .foregroundStyle(.primary)
            if let cls {
                // AI label takes precedence over the raw user/cmd
                Text(cls.label)
                    .font(.footnote.bold())
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(cls.kind)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.tertiary)
                ConfidenceChip(confidence: cls.confidence)
            } else {
                if let u = svc.user, !u.isEmpty, u != "0" {
                    Text("· \(u)")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                if let cmd = svc.command, !cmd.isEmpty {
                    Text(cmd)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                } else if let probe = svc.probe, !probe.isEmpty {
                    Text(probe.replacingOccurrences(of: "\n", with: " · "))
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 1)
        .help(cls?.reasoning ?? (svc.probe ?? ""))
    }

    private func runClassification() async {
        guard !classifying else { return }
        classifying = true
        classifyError = nil
        defer { classifying = false }
        do {
            classifications = try await client.classify(ports)
        } catch {
            classifyError = error.localizedDescription
        }
    }
}

private struct ConfidenceChip: View {
    let confidence: String
    var body: some View {
        Text(confidence.lowercased())
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(color)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(color.opacity(0.18))
            .clipShape(Capsule())
    }
    private var color: Color {
        switch confidence.lowercased() {
        case "high":   return .green
        case "medium": return .yellow
        case "low":    return .orange
        default:       return .secondary
        }
    }
}

private struct ServiceRowView: View {
    let row: ServicesViewModel.Row
    let inFlight: Bool
    let onAction: (ServiceAction) -> Void

    var body: some View {
        HStack(spacing: 10) {
            // Pulsing dot during in-flight so the operator sees motion
            // immediately — no more "I clicked Start and nothing
            // happened for 15 seconds".
            Circle()
                .fill(inFlight ? Color.accentColor : stateColor)
                .frame(width: 8, height: 8)
                .opacity(inFlight ? 0.55 : 1.0)
            Image(systemName: row.service.iconSymbol)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(width: 18)
            Text(row.service.displayName)
                .font(.subheadline.bold())
                .foregroundStyle(.primary)
                .lineLimit(1)
            // Override the summary while in-flight so the operator sees
            // the row is working. Once the prober confirms the new
            // state, inFlight clears and the real summary returns.
            if inFlight {
                Text("working…")
                    .font(.footnote)
                    .foregroundStyle(Color.accentColor)
                    .lineLimit(1)
            } else if let summary = row.status.summary {
                Text(summary)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 4)
            if inFlight {
                ProgressView().controlSize(.mini)
            } else if let action = row.service.action(for: row.status.state) {
                Button(action.label) { onAction(action) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .tint(buttonTint(for: action))
            }
        }
        .padding(.vertical, 1)
        .help(row.status.error ?? row.service.displayName)
    }

    private var stateColor: Color {
        switch row.status.state {
        case .up:      return .green
        case .down:    return .red
        case .unknown: return .secondary
        }
    }

    /// Subtle button color so destructive actions read distinct from
    /// recoveries — Stop reads slightly muted, Start/Restart accent-tinted.
    private func buttonTint(for action: ServiceAction) -> Color {
        switch action.command {
        case .stopVLLM:                 return .secondary
        case .startVLLM,
             .sshRestartUnit:           return .accentColor
        case .openURL:                  return .primary
        }
    }
}

private struct ServiceToastView: View {
    let toast: ServicesViewModel.Toast
    let onDismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: toast.isError
                  ? "exclamationmark.triangle.fill"
                  : "checkmark.circle.fill")
                .foregroundStyle(toast.isError ? .red : .green)
                .font(.caption)
                .padding(.top, 1)
            Text(toast.message)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(3)
            Spacer(minLength: 4)
            Button { onDismiss() } label: {
                Image(systemName: "xmark")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill((toast.isError ? Color.red : Color.green).opacity(0.10))
        )
    }
}

@MainActor
final class ServicesViewModel: ObservableObject {
    struct Row: Identifiable {
        let service: Service
        var status: ServiceStatus
        var id: String { service.id }
    }
    struct Toast: Equatable {
        let message: String
        let isError: Bool
    }

    @Published private(set) var rows: [Row] = []
    @Published private(set) var unknownPorts: [RoccoStatus.Service] = []
    @Published private(set) var toast: Toast?
    @Published private(set) var isBusy: Bool = false
    /// Service IDs currently waiting on the next snapshot to confirm
    /// their action took effect. Used to overlay a "starting…" /
    /// "stopping…" hint in the row so the operator gets immediate
    /// visual feedback instead of staring at the unchanged state
    /// until the next 15-second poll fires.
    @Published private(set) var inFlight: Set<String> = []

    private let prober = ServiceProber()
    private let runner: ServiceCommandRunner
    private var toastDismissTask: Task<Void, Never>?
    private weak var store: StatusStore?

    init(runner: ServiceCommandRunner = DefaultServiceCommandRunner()) {
        self.runner = runner
    }

    /// SwiftUI calls this from .task — we capture the store so the
    /// action handler can trigger a fresh poll without depending on
    /// the 15s timer for visual feedback.
    func bind(store: StatusStore) { self.store = store }

    func refresh(snapshot: RoccoStatus?) async {
        isBusy = true
        defer { isBusy = false }
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: snapshot?.services ?? [])
        // Synthesize cheap rows IMMEDIATELY so the section shows
        // something even before the SSH/HTTP probes complete (~5s
        // worst-case). Then update each row's status as the probe
        // finishes — but since SwiftUI redraws on every @Published
        // mutation, batching at the end is cheaper. Compromise:
        // publish a "checking…" pass first, then the real states.
        rows = result.known.services.map {
            Row(service: $0,
                status: ServiceStatus(state: .unknown, summary: "checking…"))
        }
        unknownPorts = result.unknown

        var newRows: [Row] = []
        for svc in result.known.services {
            let status = await prober.probe(svc, snapshot: snapshot)
            newRows.append(Row(service: svc, status: status))
        }
        rows = newRows
    }

    /// Run the chosen action with immediate visual feedback. Three
    /// reasons the previous version felt "very dumb" per the user:
    ///   1. UI didn't update — only the prober cache got invalidated,
    ///      so the row stayed in its pre-action state until the next
    ///      15-second poll fired.
    ///   2. No in-flight indicator — clicking Start looked identical
    ///      to not clicking it.
    ///   3. Big actions (vLLM start) take 30+ seconds to actually
    ///      bind their port (Kimi-72B has 145 GB of weights to load
    ///      from disk into VRAM), but the first re-poll fires after
    ///      ~0s and reports "still down".
    ///
    /// Fix: mark the row in-flight, fire an immediate snapshot
    /// refresh, AND schedule staggered follow-up refreshes at
    /// 3s / 8s / 30s to catch slow lifts. Clear in-flight when the
    /// service finally reports `.up`.
    func perform(action: ServiceAction, on service: Service) async {
        isBusy = true
        inFlight.insert(service.id)
        defer { isBusy = false }
        do {
            let summary = try await runner.perform(action.command)
            prober.invalidate()
            showToast(.init(message: summary, isError: false))
            await pollUntilStateSettles(for: service, expectingUp: isUpAction(action))
        } catch {
            inFlight.remove(service.id)
            showToast(.init(message: error.localizedDescription, isError: true))
        }
    }

    /// Whether the action is supposed to bring the service .up
    /// (Start / Open / Restart) vs take it .down (Stop). Used to
    /// decide when the in-flight overlay should clear.
    private func isUpAction(_ action: ServiceAction) -> Bool {
        switch action.command {
        case .stopVLLM:                          return false
        case .startVLLM, .sshRestartUnit,
             .openURL:                           return true
        }
    }

    /// Trigger snapshot refreshes at staggered offsets so the row
    /// catches state changes that take a while — vLLM in particular
    /// needs ~30s to load 145 GB of weights. Stops early once the
    /// service reaches the expected state.
    private func pollUntilStateSettles(for service: Service,
                                        expectingUp: Bool) async {
        let offsetsSec: [UInt64] = [0, 3, 8, 30]
        for delay in offsetsSec {
            if delay > 0 {
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            }
            // ask the StatusStore to re-poll Rocco so vllm.running flips
            await store?.refresh()
            prober.invalidate()
            await refresh(snapshot: store?.snapshot)
            let settled = rows.first { $0.service.id == service.id }
                .map { $0.status.state == .up } ?? false
            if settled == expectingUp {
                inFlight.remove(service.id)
                return
            }
        }
        // 30s passed and we're still not in the expected state —
        // give up the in-flight overlay (the natural poll loop will
        // eventually flip it). Surface a quiet warning so the user
        // knows we tried but it didn't take.
        inFlight.remove(service.id)
        if !rows.isEmpty {
            showToast(.init(
                message: "Action sent but \(service.displayName) hasn't "
                    + (expectingUp ? "come up" : "stopped")
                    + " yet — check `ssh rocco journalctl --user -u rocco-agent`",
                isError: true))
        }
    }

    func dismissToast() {
        toastDismissTask?.cancel()
        toast = nil
    }

    private func showToast(_ t: Toast) {
        toast = t
        toastDismissTask?.cancel()
        // Auto-dismiss success toasts after 4s; errors stick so the user
        // can read + copy them.
        if !t.isError {
            toastDismissTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                guard !Task.isCancelled else { return }
                await MainActor.run { self?.toast = nil }
            }
        }
    }
}
