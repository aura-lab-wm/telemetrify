import SwiftUI
import AppKit
import RoccoPulseCore

@MainActor
final class LogInspectorWindowController {
    static let shared = LogInspectorWindowController()
    private var windows: [String: NSWindow] = [:]
    private var delegates: [String: WindowCloseDelegate] = [:]

    func show(service: Service) {
        let key = service.id
        if let existing = windows[key] {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 980, height: 620),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "\(service.displayName) Logs"
        window.center()
        window.contentView = NSHostingView(
            rootView: LogInspectorView(serviceName: service.displayName,
                                       logFiles: service.logFiles)
        )
        window.isReleasedWhenClosed = false
        windows[key] = window
        let delegate = WindowCloseDelegate { [weak self] in
            self?.windows.removeValue(forKey: key)
            self?.delegates.removeValue(forKey: key)
        }
        delegates[key] = delegate
        window.delegate = delegate
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

private final class WindowCloseDelegate: NSObject, NSWindowDelegate {
    private let onClose: () -> Void

    init(onClose: @escaping () -> Void) {
        self.onClose = onClose
    }

    func windowWillClose(_ notification: Notification) {
        onClose()
    }
}

struct LogInspectorView: View {
    let serviceName: String
    let logFiles: [Service.LogFile]
    @StateObject private var model = LogInspectorModel()
    @State private var selectedID: String?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                rawLogPane
                    .frame(minWidth: 420)
                Divider()
                insightPane
                    .frame(minWidth: 360)
            }
        }
        .frame(minWidth: 820, minHeight: 520)
        .task {
            selectedID = selectedID ?? logFiles.first?.id
            await loadSelected()
        }
        .onChange(of: selectedID) { _, _ in
            Task { await loadSelected() }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "terminal")
                .font(.title3)
                .foregroundStyle(Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text(serviceName)
                    .font(.headline)
                Text("AI-Pulse log inspector")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Picker("Log", selection: $selectedID) {
                ForEach(logFiles) { file in
                    Text(file.label).tag(Optional(file.id))
                }
            }
            .pickerStyle(.menu)
            .frame(width: 180)
            Button {
                Task { await loadSelected() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .controlSize(.small)
        }
        .padding(14)
    }

    private var rawLogPane: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("LOG")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.tertiary)
                .tracking(1)
            if model.isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    Text(model.text.isEmpty ? "No log lines found." : model.text)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .background(Color.primary.opacity(0.045))
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
        }
        .padding(14)
    }

    private var insightPane: some View {
        let insight = model.insight
        return VStack(alignment: .leading, spacing: 12) {
            Text("QUICK READ")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.tertiary)
                .tracking(1)

            Text(insight.summary)
                .font(.title3.bold())
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 10) {
                InsightChip(label: "Errors", value: insight.errors, color: .red)
                InsightChip(label: "Warnings", value: insight.warnings, color: .orange)
                InsightChip(label: "Starts", value: insight.starts, color: .green)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("Signal Mix")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                SeverityBar(label: "error", value: insight.errors, total: insight.total, color: .red)
                SeverityBar(label: "warn", value: insight.warnings, total: insight.total, color: .orange)
                SeverityBar(label: "info", value: insight.info, total: insight.total, color: .blue)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Likely Cause")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                Text(insight.cause)
                    .font(.body)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Recent Notables")
                    .font(.caption.bold())
                    .foregroundStyle(.secondary)
                ForEach(insight.notables, id: \.self) { line in
                    Text(line)
                        .font(.system(size: 11, design: .monospaced))
                        .lineLimit(2)
                        .truncationMode(.middle)
                        .textSelection(.enabled)
                        .padding(.vertical, 2)
                }
            }
            Spacer()
        }
        .padding(14)
    }

    private func loadSelected() async {
        guard let id = selectedID,
              let file = logFiles.first(where: { $0.id == id })
        else { return }
        await model.load(file)
    }
}

@MainActor
private final class LogInspectorModel: ObservableObject {
    @Published var text: String = ""
    @Published var isLoading: Bool = false
    @Published var insight = LogInsight.empty
    private let launcher = RealProcessLauncher()

    func load(_ file: Service.LogFile) async {
        isLoading = true
        defer { isLoading = false }
        do {
            let loaded = try await loadText(file)
            text = loaded
            insight = LogInsight.analyze(loaded)
        } catch {
            text = error.localizedDescription
            insight = LogInsight(
                total: 1,
                errors: 1,
                warnings: 0,
                starts: 0,
                info: 0,
                summary: "Could not read this log.",
                cause: error.localizedDescription,
                notables: [error.localizedDescription]
            )
        }
    }

    private func loadText(_ file: Service.LogFile) async throws -> String {
        switch file.location {
        case .local(let path):
            return try tailLocal(path: path)
        case .remote(let host, let path):
            return try await Task.detached { [launcher] in
                let result = try launcher.run(
                    executable: "/usr/bin/ssh",
                    arguments: [host, path],
                    timeout: 8
                )
                guard result.exitCode == 0 else {
                    throw LogInspectorError.commandFailed(result.stderr)
                }
                return result.stdout
            }.value
        }
    }

    private func tailLocal(path: String) throws -> String {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        let full = String(data: data, encoding: .utf8) ?? ""
        return full.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
            .suffix(400)
            .joined(separator: "\n")
    }
}

private enum LogInspectorError: LocalizedError {
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .commandFailed(let stderr):
            return stderr.isEmpty ? "Log command failed." : stderr
        }
    }
}

private struct LogInsight: Equatable {
    let total: Int
    let errors: Int
    let warnings: Int
    let starts: Int
    let info: Int
    let summary: String
    let cause: String
    let notables: [String]

    static let empty = LogInsight(
        total: 0,
        errors: 0,
        warnings: 0,
        starts: 0,
        info: 0,
        summary: "No log loaded yet.",
        cause: "Pick a log source or refresh the current one.",
        notables: []
    )

    static func analyze(_ text: String) -> LogInsight {
        let lines = text.split(whereSeparator: \.isNewline).map(String.init)
        guard !lines.isEmpty else { return empty }
        let lower = lines.map { $0.lowercased() }
        let errors = lower.filter {
            $0.contains("error") || $0.contains("failed") || $0.contains("traceback") || $0.contains("exception")
        }.count
        let warnings = lower.filter {
            $0.contains("warn") || $0.contains("timeout") || $0.contains("retry") || $0.contains("unavailable")
        }.count
        let starts = lower.filter {
            $0.contains("start") || $0.contains("listening") || $0.contains("running") || $0.contains("loaded")
        }.count
        let notables = lines.filter { line in
            let l = line.lowercased()
            return l.contains("error") || l.contains("failed") || l.contains("warn")
                || l.contains("timeout") || l.contains("restart") || l.contains("started")
        }.suffix(8)

        let summary: String
        let cause: String
        if errors > 0 {
            summary = "Action is failing or exiting noisily."
            cause = "The latest tail contains \(errors) error-shaped line\(errors == 1 ? "" : "s"). Start with the newest notable line and work backward to the first failure."
        } else if warnings > 0 {
            summary = "Service is running with recoverable warnings."
            cause = "Warnings, retries, or timeouts appear without a hard failure. The service may be degraded or waiting on a dependency."
        } else if starts > 0 {
            summary = "Service looks recently active."
            cause = "Startup/running signals are present and no obvious error lines were found in the current tail."
        } else {
            summary = "No clear failure signal in the current tail."
            cause = "The log is quiet or mostly neutral. Refresh after reproducing the action for a stronger read."
        }

        return LogInsight(
            total: lines.count,
            errors: errors,
            warnings: warnings,
            starts: starts,
            info: max(0, lines.count - errors - warnings),
            summary: summary,
            cause: cause,
            notables: Array(notables)
        )
    }
}

private struct InsightChip: View {
    let label: String
    let value: Int
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
            Text("\(value)")
                .font(.title2.bold())
                .foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(color.opacity(0.11))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct SeverityBar: View {
    let label: String
    let value: Int
    let total: Int
    let color: Color

    var body: some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 42, alignment: .leading)
            GeometryReader { geo in
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(Color.primary.opacity(0.08))
                    .overlay(alignment: .leading) {
                        RoundedRectangle(cornerRadius: 4, style: .continuous)
                            .fill(color)
                            .frame(width: geo.size.width * fraction)
                    }
            }
            .frame(height: 8)
            Text("\(value)")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 28, alignment: .trailing)
        }
    }

    private var fraction: Double {
        guard total > 0 else { return 0 }
        return min(1, Double(value) / Double(total))
    }
}
