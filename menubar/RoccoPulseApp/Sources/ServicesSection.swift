import SwiftUI
import RoccoPulseCore

/// Compact "Services" list rendered between the GPU rows and the lifecycle
/// banner. Each row shows: status dot, icon, name, status summary, and the
/// FIRST action whose `showWhen` matches the live state — so a `.down`
/// service shows "Start" / "Restart", a `.up` one shows "Stop", etc. Open
/// always wins for http-kind services because the URL is a valid recovery
/// path regardless of status.
struct ServicesSection: View {
    enum Scope {
        case rocco
        case local
    }

    @EnvironmentObject var store: StatusStore
    @StateObject private var model = ServicesViewModel()
    let scope: Scope

    init(scope: Scope = .rocco) {
        self.scope = scope
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text("SERVICES")
                    .font(.system(.caption, design: .monospaced))
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
            VStack(spacing: 2) {
                ForEach(model.rows) { row in
                    ServiceRowView(
                        row: row,
                        inFlight: model.inFlight.contains(row.service.id),
                        logLines: model.actionLogs[row.service.id] ?? [],
                        modelPicker: modelPicker(for: row.service),
                        trainingHint: trainingHint(for: row.service)
                    ) { action in
                        Task { await model.perform(action: action, on: row.service) }
                    } onInspectLogs: {
                        LogInspectorWindowController.shared.show(service: row.service)
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
            await model.refresh(snapshot: store.snapshot, scope: scope.serviceScope)
        }
    }

    /// Build the model picker for the vllm row from the latest snapshot's
    /// `models` block. Returns nil for every other service (and when the
    /// agent is pre-v4 / hasn't reported models yet), so the dropdown only
    /// appears on the vllm row. Selecting fires a synthetic `.selectModel`
    /// action through the same perform() path as Start/Stop so it gets the
    /// in-flight overlay + staggered re-poll for free.
    private func modelPicker(for service: Service) -> ServiceRowView.ModelPicker? {
        guard service.id == "vllm",
              let models = store.snapshot?.models,
              !models.available.isEmpty
        else { return nil }
        return ServiceRowView.ModelPicker(
            available: models.available,
            selected: models.selectedProfile
        ) { profile in
            let action = ServiceAction(
                label: "Model",
                showWhen: [.up, .down, .unknown],
                command: .selectModel(profile: profile))
            Task { await model.perform(action: action, on: service) }
        }
    }

    /// "capped by training" note for the vLLM row when Auto landed on a
    /// smaller model because AURA Pulse sees a training job eating GPUs.
    private func trainingHint(for service: Service) -> String? {
        guard service.id == "vllm",
              store.snapshot?.isAutoCappedByTraining == true
        else { return nil }
        return "capped by training"
    }
}

private extension ServicesSection.Scope {
    var serviceScope: Service.Scope {
        switch self {
        case .rocco: return .rocco
        case .local: return .local
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
    private enum Metrics {
        static let dot: CGFloat = 10
        static let icon: CGFloat = 22
        static let name: CGFloat = 108
        static let spotlight: CGFloat = 34   // in-card, ALWAYS reserved
        static let gutter: CGFloat = 44      // outside-card action column
        static let rowHeight: CGFloat = 32
        static let columnGap: CGFloat = 8
        static let gutterButton: CGFloat = 32   // inner control size inside the 44pt gutter
    }

    /// vLLM-only: the pinnable model configs + current selection, plus a
    /// handler invoked when the operator picks one (nil = Auto). Rendered
    /// as a dropdown in the row; nil for every non-vllm service.
    struct ModelPicker {
        let available: [RoccoStatus.Models.Available]
        let selected: Int?            // nil = auto
        let onSelect: (Int?) -> Void
    }

    let row: ServicesViewModel.Row
    let inFlight: Bool
    let logLines: [String]
    var modelPicker: ModelPicker? = nil
    /// vLLM-only: a short note (e.g. "capped by training") explaining why Auto
    /// landed on a smaller model. nil hides it.
    var trainingHint: String? = nil
    let onAction: (ServiceAction) -> Void
    let onInspectLogs: () -> Void

    var body: some View {
        // Card + gutter are SIBLINGS. The lifecycle action never enters
        // the card, so the card's frame is identical whether the service
        // is up, down, or transitioning — that's the whole point.
        HStack(alignment: .center, spacing: 8) {
            card
            actionGutter
                .frame(width: Metrics.gutter)
        }
        .padding(.vertical, 2)
        .help(row.status.error ?? row.service.displayName)
    }

    /// Everything that belongs to the service itself — status, identity,
    /// summary, model chip, log strip — wrapped in one tinted container.
    /// Its geometry must be identical in every lifecycle state AND across
    /// services: the model picker is a fixed-height chip ON the primary
    /// line (design Option B), never a second row.
    private var card: some View {
        VStack(alignment: .leading, spacing: 4) {
            primaryRow
            if !logLines.isEmpty {
                ServiceLogStrip(lines: logLines, isLive: inFlight)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(0.05))
        )
    }

    private var primaryRow: some View {
        HStack(alignment: .center, spacing: Metrics.columnGap) {
            statusDot
            serviceIcon
            serviceName
                .frame(width: Metrics.name, alignment: .leading)
            summaryText
                .frame(maxWidth: .infinity, alignment: .leading)
            trainingHintView
            modelChipView
            spotlightButton   // pinned trailing IN-CARD, same x every row
        }
        .frame(minHeight: Metrics.rowHeight)
    }

    /// The log-inspect button. When a service has no log files we still
    /// reserve the exact same width so the trailing edge never drifts.
    @ViewBuilder
    private var spotlightButton: some View {
        if row.service.logFiles.isEmpty {
            Color.clear
                .frame(width: Metrics.spotlight, height: 22)
        } else {
            Button { onInspectLogs() } label: {
                Image(systemName: "text.magnifyingglass")
                    .font(.system(size: 13, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .frame(width: Metrics.spotlight - 6, height: 22)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .frame(width: Metrics.spotlight)
            .help("Inspect logs")
            .accessibilityLabel("Inspect \(row.service.displayName) logs")
        }
    }

    /// Fixed-width lifecycle column OUTSIDE the card. Icon-only.
    /// Switches on the Core-tested GutterPresentation so this view has
    /// zero mapping logic of its own.
    @ViewBuilder
    private var actionGutter: some View {
        let currentAction = row.service.action(for: row.status.state)
        switch GutterPresentation.make(action: currentAction, inFlight: inFlight) {
        case .busy:
            ProgressView()
                .controlSize(.small)
                .frame(width: Metrics.gutterButton, height: Metrics.gutterButton)
        case .placeholder:
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.10),
                              style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                .frame(width: Metrics.gutterButton, height: Metrics.gutterButton)
                .accessibilityHidden(true)
        case .action(let symbol, let verb, let isDestructive):
            let extras = Array(row.service.actions(for: row.status.state).dropFirst())
            Button {
                // currentAction is non-nil whenever GutterPresentation returns .action; the if-let is belt-and-braces, not a reachable branch.
                if let currentAction { onAction(currentAction) }
            } label: {
                Image(systemName: symbol)
                    .font(.system(size: 13, weight: .bold))
                    .frame(width: 26, height: 26)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(isDestructive ? .red : .accentColor)
            .help("\(verb) \(row.service.displayName)"
                  + (extras.isEmpty ? "" : " — right-click for \(extras.map(\.label).joined(separator: "/"))"))
            .accessibilityLabel("\(verb) \(row.service.displayName)")
            .contextMenu { secondaryMenuItems(extras) }
        }
    }

    /// Secondary lifecycle actions (Restart, Kill, …) for the current
    /// state — everything after the gutter's primary. A context menu
    /// keeps destructive escalations one deliberate right-click away
    /// instead of widening the 44pt gutter.
    @ViewBuilder
    private func secondaryMenuItems(_ extras: [ServiceAction]) -> some View {
        ForEach(Array(extras.enumerated()), id: \.offset) { _, extra in
            if case .action(let symbol, _, let isDestructive) =
                GutterPresentation.make(action: extra, inFlight: false) {
                Button(role: isDestructive ? .destructive : nil) {
                    onAction(extra)
                } label: {
                    Label(extra.label, systemImage: symbol)
                }
            }
        }
    }

    private var statusDot: some View {
        Circle()
            .fill(inFlight ? Color.accentColor : stateColor)
            .frame(width: Metrics.dot, height: Metrics.dot)
            .opacity(inFlight ? 0.55 : 1.0)
    }

    private var serviceIcon: some View {
        Image(systemName: row.service.iconSymbol)
            .font(.system(size: 17, weight: .medium))
            .foregroundStyle(.secondary)
            .symbolRenderingMode(.hierarchical)
            .frame(width: Metrics.icon, height: Metrics.rowHeight)
    }

    @ViewBuilder
    private var serviceName: some View {
        if let clientURL = row.service.clientURL {
            Button(row.service.displayName) {
                onAction(ServiceAction(label: "Open", showWhen: [],
                                       command: .openURL(clientURL)))
            }
            .buttonStyle(.link)
            .font(.body.bold())
            .lineLimit(1)
            .truncationMode(.tail)
            .help("Open \(clientURL.absoluteString)")
        } else {
            Text(row.service.displayName)
                .font(.body.bold())
                .foregroundStyle(.primary)
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }

    @ViewBuilder
    private var summaryText: some View {
        if inFlight {
            Text("working...")
                .font(.callout)
                .foregroundStyle(Color.accentColor)
                .lineLimit(1)
        } else if let summary = row.status.summary {
            Text(summary)
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    /// "Capped by training" now reads as a compact orange glyph on the
    /// primary line (tooltip carries the explanation) so it can never
    /// add a second row.
    @ViewBuilder
    private var trainingHintView: some View {
        if !inFlight, trainingHint != nil {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10))
                .foregroundStyle(.orange)
                .help("AURA Pulse reports a training job is using GPUs, so Auto picked a smaller model.")
                .accessibilityLabel("Model capped by training")
        }
    }

    /// Option B chip (design-explorations/vllm-model-picker-options.html):
    /// a fixed-height capsule on the PRIMARY line that owns the model
    /// identity and opens the picker. Single line + capped width means
    /// it can never change the card's shape. Picking a model writes the
    /// override on Rocco and recycles vLLM (~75s in-flight).
    @ViewBuilder
    private var modelChipView: some View {
        if !inFlight, let mp = modelPicker, !mp.available.isEmpty {
            Menu {
                Button { mp.onSelect(nil) } label: {
                    Text((mp.selected == nil ? "✓ " : "   ") + "Auto (by free GPUs)")
                }
                Divider()
                ForEach(mp.available) { m in
                    Button { mp.onSelect(m.profile) } label: {
                        Text((mp.selected == m.profile ? "✓ " : "   ")
                             + m.label
                             + (m.downloaded ? "" : "  (not downloaded)"))
                    }
                    .disabled(!m.downloaded)
                }
            } label: {
                HStack(spacing: 3) {
                    Text(currentModelShortLabel(mp))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 8, weight: .bold))
                }
                // 12pt matches the pre-chip picker — the REST of the row
                // scaled up to meet it instead (user call: never shrink
                // the selector text).
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.accentColor)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.accentColor.opacity(0.14)))
            .frame(maxWidth: 150)
            .fixedSize(horizontal: false, vertical: true)
            .help("Choose which model vLLM serves on Rocco")
            .accessibilityLabel("Model: \(currentModelShortLabel(mp))")
        }
    }

    /// Chip resting label: "Auto", or the pinned model via the Core-tested
    /// ModelChip abbreviation (e.g. "WRN-70B · BF16").
    private func currentModelShortLabel(_ mp: ModelPicker) -> String {
        guard let sel = mp.selected,
              let m = mp.available.first(where: { $0.profile == sel })
        else { return "Auto" }
        return ModelChip.label(model: m.model, precision: m.precision)
    }

    private var stateColor: Color {
        switch row.status.state {
        case .up:      return .green
        case .down:    return .red
        case .unknown: return .secondary
        }
    }

}

private struct ServiceLogStrip: View {
    let lines: [String]
    let isLive: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: "terminal")
                    .font(.system(size: 10))
                Text(isLive ? "live logs" : "last action")
                    .font(.system(size: 10, design: .monospaced))
                Spacer(minLength: 0)
            }
            .foregroundStyle(isLive ? Color.accentColor : Color.secondary)
            ForEach(displayLines, id: \.self) { line in
                Text(line)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }
        }
        .padding(.leading, 36)
        .padding(.trailing, 6)
        .padding(.vertical, 5)
        // no own background — the card already tints this area
    }

    private var displayLines: [String] {
        Array(lines.suffix(8))
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
    @Published private(set) var actionLogs: [String: [String]] = [:]
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

    func refresh(snapshot: RoccoStatus?, scope: Service.Scope) async {
        isBusy = true
        defer { isBusy = false }
        let result = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: snapshot?.services ?? [])
        let services = result.known.services.filter { $0.scope == scope }
        // Synthesize cheap rows IMMEDIATELY so the section shows
        // something even before the SSH/HTTP probes complete (~5s
        // worst-case). Then update each row's status as the probe
        // finishes — but since SwiftUI redraws on every @Published
        // mutation, batching at the end is cheaper. Compromise:
        // publish a "checking…" pass first, then the real states.
        rows = services.map {
            Row(service: $0,
                status: ServiceStatus(state: .unknown, summary: "checking…"))
        }
        unknownPorts = scope == .rocco ? result.unknown : []

        var newRows: [Row] = []
        for svc in services {
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
        actionLogs[service.id] = []
        appendLog("queued \(action.label)", for: service.id)
        defer { isBusy = false }
        do {
            let summary = try await runner.perform(action.command) { [weak self] line in
                Task { @MainActor in
                    self?.appendLog(line, for: service.id)
                }
            }
            appendLog(summary, for: service.id)
            prober.invalidate()
            showToast(.init(message: summary, isError: false))
            await pollUntilStateSettles(for: service, expectingUp: isUpAction(action))
        } catch {
            inFlight.remove(service.id)
            appendLog("error: \(error.localizedDescription)", for: service.id)
            showToast(.init(message: error.localizedDescription, isError: true))
        }
    }

    /// Whether the action is supposed to bring the service .up
    /// (Start / Open / Restart) vs take it .down (Stop). Used to
    /// decide when the in-flight overlay should clear.
    private func isUpAction(_ action: ServiceAction) -> Bool {
        switch action.command {
        case .stopVLLM, .stopLocalAgent,
             .sshStopUnit, .sshKillUnit,
             .killLocalAgent, .quitLocalApp:      return false
        case .startVLLM, .sshRestartUnit,
             .openURL, .selectModel,
             .startLocalAgent, .restartLocalAgent,
             .sshStartUnit:                        return true
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
            await refresh(snapshot: store?.snapshot, scope: service.scope)
            let settled = rows.first { $0.service.id == service.id }
                .map { $0.status.state == .up } ?? false
            appendLog("poll +\(delay)s: \(settled ? "up" : "not ready")", for: service.id)
            if settled == expectingUp {
                inFlight.remove(service.id)
                appendLog("settled", for: service.id)
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

    private func appendLog(_ line: String, for serviceID: String) {
        let clean = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        var lines = actionLogs[serviceID] ?? []
        lines.append(clean)
        actionLogs[serviceID] = Array(lines.suffix(80))
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
