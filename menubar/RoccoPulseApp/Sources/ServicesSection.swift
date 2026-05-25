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
                    ServiceRowView(row: row) { action in
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
            await model.refresh(snapshot: store.snapshot)
        }
    }
}

private struct UnknownPortsDisclosure: View {
    let ports: [RoccoStatus.Service]
    @State private var isExpanded: Bool = false

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            ScrollView(.vertical, showsIndicators: true) {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(ports, id: \.id) { svc in
                        HStack(spacing: 6) {
                            Circle().fill(Color.secondary)
                                .frame(width: 5, height: 5)
                            Text("port \(String(svc.port))")
                                .font(.system(.caption2, design: .monospaced))
                                .foregroundStyle(.primary)
                            if let u = svc.user, !u.isEmpty, u != "0" {
                                Text("· \(u)")
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                            }
                            if let cmd = svc.command, !cmd.isEmpty {
                                Text(cmd)
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                            Spacer(minLength: 0)
                        }
                    }
                }
            }
            .frame(maxHeight: 140)
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
}

private struct ServiceRowView: View {
    let row: ServicesViewModel.Row
    let onAction: (ServiceAction) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(stateColor)
                .frame(width: 8, height: 8)
            Image(systemName: row.service.iconSymbol)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(width: 18)
            Text(row.service.displayName)
                .font(.subheadline.bold())
                .foregroundStyle(.primary)
                .lineLimit(1)
            if let summary = row.status.summary {
                Text(summary)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 4)
            if let action = row.service.action(for: row.status.state) {
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

    private let prober = ServiceProber()
    private let runner: ServiceCommandRunner
    private var toastDismissTask: Task<Void, Never>?

    init(runner: ServiceCommandRunner = DefaultServiceCommandRunner()) {
        self.runner = runner
    }

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

    /// Run the chosen action and reflect the result as a transient toast.
    /// The prober cache is invalidated on success so the next render shows
    /// the post-action state.
    func perform(action: ServiceAction, on service: Service) async {
        isBusy = true
        defer { isBusy = false }
        do {
            let summary = try await runner.perform(action.command)
            prober.invalidate()
            showToast(.init(message: summary, isError: false))
        } catch {
            showToast(.init(message: error.localizedDescription, isError: true))
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
