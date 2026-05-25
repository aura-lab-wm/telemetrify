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

            VStack(spacing: 4) {
                ForEach(model.rows) { row in
                    ServiceRowView(row: row) { action in
                        Task { await model.perform(action: action, on: row.service) }
                    }
                }
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

private struct ServiceRowView: View {
    let row: ServicesViewModel.Row
    let onAction: (ServiceAction) -> Void

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(stateColor)
                .frame(width: 7, height: 7)
            Image(systemName: row.service.iconSymbol)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 16)
            Text(row.service.displayName)
                .font(.caption.bold())
                .foregroundStyle(.primary)
                .lineLimit(1)
            if let summary = row.status.summary {
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 4)
            if let action = row.service.action(for: row.status.state) {
                Button(action.label) { onAction(action) }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .tint(buttonTint(for: action))
            }
        }
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
    @Published private(set) var toast: Toast?
    @Published private(set) var isBusy: Bool = false

    private let prober = ServiceProber()
    private let runner: ServiceCommandRunner
    private var toastDismissTask: Task<Void, Never>?

    init(runner: ServiceCommandRunner = DefaultServiceCommandRunner()) {
        self.runner = runner
    }

    func refresh(snapshot: RoccoStatus?) async {
        let registry = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: snapshot?.services ?? [])
        var newRows: [Row] = []
        for svc in registry.services {
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
