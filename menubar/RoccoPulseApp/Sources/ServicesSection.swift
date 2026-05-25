import SwiftUI
import RoccoPulseCore

/// Compact "Services" list rendered between the GPU rows and the lifecycle
/// banner. Each row shows: status dot, icon, name, status summary, action.
///
/// State is computed once on appear + on every snapshot tick (we trigger a
/// re-probe in the .task by observing `store.lastFetchedAt`). HTTP probes
/// run on a background queue; from-status reads are synchronous.
struct ServicesSection: View {
    @EnvironmentObject var store: StatusStore
    @StateObject private var model = ServicesViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("SERVICES")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.tertiary)
                .tracking(1.0)

            VStack(spacing: 4) {
                ForEach(model.rows) { row in
                    ServiceRowView(row: row)
                }
            }
        }
        .task(id: store.lastFetchedAt) {
            await model.refresh(snapshot: store.snapshot)
        }
    }
}

private struct ServiceRowView: View {
    let row: ServicesViewModel.Row

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
            if let url = row.service.clientURL {
                Button("Open") { NSWorkspace.shared.open(url) }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
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
}

@MainActor
final class ServicesViewModel: ObservableObject {
    struct Row: Identifiable {
        let service: Service
        var status: ServiceStatus
        var id: String { service.id }
    }
    @Published private(set) var rows: [Row] = []
    private let prober = ServiceProber()

    func refresh(snapshot: RoccoStatus?) async {
        let registry = ServiceRegistry(services: ServiceRegistry.builtins())
            .merging(discovered: snapshot?.services ?? [])
        var newRows: [Row] = []
        // Probe sequentially — we have ≤8 services typically and the prober
        // caches for 5s, so this won't block meaningfully after the first
        // tick. Parallelizing would complicate the cache locking.
        for svc in registry.services {
            let status = await prober.probe(svc, snapshot: snapshot)
            newRows.append(Row(service: svc, status: status))
        }
        rows = newRows
    }
}
