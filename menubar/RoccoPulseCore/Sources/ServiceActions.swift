import Foundation

/// What the operator can do to a service from the popover. Each command is
/// a typed value (not a free-form closure) so:
///   1. tests can assert the right command was constructed without
///      executing real SSH or HTTP
///   2. a future "audit log" can record exactly which commands fired
///   3. UI buttons map to commands without owning their implementation
public enum ServiceCommand: Equatable, Sendable {
    /// Hand off a URL to the system (NSWorkspace.open / xdg-open).
    case openURL(URL)
    /// `ssh <host> systemctl --user restart <unit>` — used to bring
    /// rocco-agent (or any other user-managed service) back from .down.
    case sshRestartUnit(host: String, unit: String)
    /// Start vLLM on Rocco — invokes the existing LifecycleCommands path.
    case startVLLM
    /// Stop vLLM on Rocco.
    case stopVLLM
    /// Pin a model profile (1...4) or `nil` for auto, then recycle vLLM so
    /// the manager relaunches with the new selection.
    case selectModel(profile: Int?)
    /// `launchctl start <label>` — kick a LOCAL (Mac) LaunchAgent. Used to
    /// bring the telemetrify UI agent up from the menubar without a terminal.
    case startLocalAgent(label: String)
    /// `launchctl stop <label>` — stop a local LaunchAgent.
    case stopLocalAgent(label: String)
    /// Bounce a local LaunchAgent: `launchctl stop` (best-effort) then
    /// `launchctl start`, mirroring `bin/service restart`.
    case restartLocalAgent(label: String)
    /// `ssh <host> systemctl --user start <unit>` — bring a remote
    /// user-unit up without bouncing it (Restart stays for recovery).
    case sshStartUnit(host: String, unit: String)
    /// `ssh <host> systemctl --user stop <unit>`.
    case sshStopUnit(host: String, unit: String)
    /// `ssh <host> systemctl --user kill -s SIGKILL <unit>` — hard kill
    /// for a wedged unit that ignores stop.
    case sshKillUnit(host: String, unit: String)
    /// `launchctl kill SIGKILL gui/<uid>/<label>` — hard kill a LOCAL
    /// LaunchAgent that stopped responding to `launchctl stop`.
    case killLocalAgent(label: String)
    /// `pkill -x <name>` — quit a local .app (e.g. Ollama) that has no
    /// LaunchAgent label to address. Exit 1 (no match) is tolerated.
    case quitLocalApp(name: String)
}

/// A button bound to a ServiceCommand, shown only when the service's
/// current `ServiceStatus.State` is in `showWhen`. The view picks the
/// first action whose predicate matches the live state and renders it —
/// keeps the row to ONE primary button so the UI stays scannable.
public struct ServiceAction: Equatable, Sendable {
    public let label: String
    public let showWhen: Set<ServiceStatus.State>
    public let command: ServiceCommand
    public let isPrimary: Bool

    public init(label: String,
                showWhen: Set<ServiceStatus.State>,
                command: ServiceCommand,
                isPrimary: Bool = true) {
        self.label = label
        self.showWhen = showWhen
        self.command = command
        self.isPrimary = isPrimary
    }

    public func applies(to state: ServiceStatus.State) -> Bool {
        showWhen.contains(state)
    }
}

/// Pluggable executor. Real impl shells out via NSWorkspace / SSH;
/// tests substitute a `RecordingServiceCommandRunner` that captures the
/// command and returns a canned result without touching the network.
public protocol ServiceCommandRunner: AnyObject, Sendable {
    /// Run the command. Returns a human-friendly success summary used by
    /// the popover toast ("vLLM start requested", "rocco-agent restarted",
    /// etc.). Throws on failure with a localized error.
    func perform(_ command: ServiceCommand) async throws -> String
    func perform(_ command: ServiceCommand, log: (@Sendable (String) -> Void)?) async throws -> String
}

public extension ServiceCommandRunner {
    func perform(_ command: ServiceCommand) async throws -> String {
        try await perform(command, log: nil)
    }
}

public final class DefaultServiceCommandRunner: ServiceCommandRunner, @unchecked Sendable {
    private let sshLauncher: ProcessLauncher
    private let lifecycle: LifecycleCommands
    private let urlOpener: @Sendable (URL) -> Bool

    public init(sshLauncher: ProcessLauncher = RealProcessLauncher(),
                lifecycle: LifecycleCommands = LifecycleCommands(),
                urlOpener: @escaping @Sendable (URL) -> Bool = DefaultServiceCommandRunner.defaultURLOpener) {
        self.sshLauncher = sshLauncher
        self.lifecycle = lifecycle
        self.urlOpener = urlOpener
    }

    public func perform(_ command: ServiceCommand, log: (@Sendable (String) -> Void)? = nil) async throws -> String {
        log?("$ \(command.displayName)")
        switch command {
        case .openURL(let url):
            if urlOpener(url) {
                log?("opened \(url.absoluteString)")
                return "Opened \(url.absoluteString)"
            }
            throw ServiceCommandError.urlOpenFailed(url)

        case .sshRestartUnit(let host, let unit):
            try runUserSystemctl(host: host, args: ["restart", unit], log: log)
            return "\(unit) restarted on \(host)"

        case .sshStartUnit(let host, let unit):
            try runUserSystemctl(host: host, args: ["start", unit], log: log)
            return "\(unit) started on \(host)"

        case .sshStopUnit(let host, let unit):
            try runUserSystemctl(host: host, args: ["stop", unit], log: log)
            return "\(unit) stopped on \(host)"

        case .sshKillUnit(let host, let unit):
            try runUserSystemctl(host: host, args: ["kill", "-s", "SIGKILL", unit], log: log)
            return "\(unit) killed on \(host)"

        case .killLocalAgent(let label):
            // launchctl kill needs the full service-target, not the bare
            // label — gui/<uid>/ is the domain LaunchAgents live in.
            _ = try runLaunchctl(["kill", "SIGKILL", "gui/\(getuid())/\(label)"], log: log)
            return "\(label) killed"

        case .quitLocalApp(let name):
            let result = try sshLauncher.run(
                executable: "/usr/bin/pkill",
                arguments: ["-x", name],
                timeout: 8)
            emit(result, to: log)
            switch result.exitCode {
            case 0:  return "\(name) stopped"
            case 1:  return "\(name) was not running"   // pkill: no match
            default:
                throw ServiceCommandError.processKillFailed(
                    code: result.exitCode,
                    stderr: result.stderr.trimmingCharacters(in: .whitespacesAndNewlines))
            }

        case .startVLLM:
            try withLifecycleLogging(log) {
                try lifecycle.startVLLM()
            }
            return "vLLM start requested"

        case .stopVLLM:
            try withLifecycleLogging(log) {
                try lifecycle.stopVLLM()
            }
            return "vLLM stop requested"

        case .selectModel(let profile):
            try withLifecycleLogging(log) {
                try lifecycle.selectModel(profile)
            }
            let name = profile.map { "profile \($0)" } ?? "Auto"
            return "Model: \(name) — restarting vLLM"

        case .startLocalAgent(let label):
            _ = try runLaunchctl(["start", label], log: log)
            return "\(label) started"

        case .stopLocalAgent(let label):
            _ = try runLaunchctl(["stop", label], log: log)
            return "\(label) stopped"

        case .restartLocalAgent(let label):
            // Stop is best-effort: the agent may already be stopped, and the
            // operation that must succeed is the start. Mirrors bin/service.
            _ = try? runLaunchctl(["stop", label], log: log)
            _ = try runLaunchctl(["start", label], log: log)
            return "\(label) restarted"
        }
    }

    /// `ssh <host> systemctl --user <args>` — shared by restart/start/
    /// stop/kill so the ssh plumbing (BatchMode, timeout, error shape)
    /// stays in one place.
    private func runUserSystemctl(host: String, args: [String],
                                  log: (@Sendable (String) -> Void)?) throws {
        let full = [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            host,
            "systemctl", "--user",
        ] + args
        let result = try sshLauncher.run(
            executable: "/usr/bin/ssh",
            arguments: full,
            timeout: 8)
        emit(result, to: log)
        guard result.exitCode == 0 else {
            throw ServiceCommandError.sshFailed(
                code: result.exitCode,
                stderr: result.stderr.trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }

    /// Run `/bin/launchctl <args>` via the injected launcher (the same seam SSH
    /// uses, so tests record the argv without forking launchctl).
    @discardableResult
    private func runLaunchctl(_ args: [String]) throws -> ProcessLaunchResult {
        let result = try sshLauncher.run(
            executable: "/bin/launchctl",
            arguments: args,
            timeout: 8)
        emit(result, to: nil)
        guard result.exitCode == 0 else {
            throw ServiceCommandError.launchctlFailed(
                code: result.exitCode,
                stderr: result.stderr.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return result
    }

    private func runLaunchctl(_ args: [String], log: (@Sendable (String) -> Void)?) throws -> ProcessLaunchResult {
        let result = try sshLauncher.run(
            executable: "/bin/launchctl",
            arguments: args,
            timeout: 8)
        emit(result, to: log)
        guard result.exitCode == 0 else {
            throw ServiceCommandError.launchctlFailed(
                code: result.exitCode,
                stderr: result.stderr.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return result
    }

    private func withLifecycleLogging(_ log: (@Sendable (String) -> Void)?,
                                      _ body: () throws -> Void) throws {
        let previous = lifecycle.delegate
        let sink = log.map { LifecycleLogSink($0) }
        lifecycle.delegate = sink
        defer { lifecycle.delegate = previous }
        try body()
    }

    private func emit(_ result: ProcessLaunchResult,
                      to log: (@Sendable (String) -> Void)?) {
        guard let log else { return }
        for line in result.stdout.logLines {
            log(line)
        }
        for line in result.stderr.logLines {
            log("stderr: \(line)")
        }
        log("exit \(result.exitCode)")
    }

    /// Real URL opener — replaced in tests by an injectable closure.
    public static let defaultURLOpener: @Sendable (URL) -> Bool = { url in
        #if canImport(AppKit)
        return NSWorkspace.shared.open(url)
        #else
        return false
        #endif
    }
}

private final class LifecycleLogSink: LifecycleOutputDelegate, @unchecked Sendable {
    private let sink: @Sendable (String) -> Void

    init(_ sink: @escaping @Sendable (String) -> Void) {
        self.sink = sink
    }

    func lifecycle(_ commands: LifecycleCommands, didEmit line: String) {
        sink(line)
    }
}

private extension ServiceCommand {
    var displayName: String {
        switch self {
        case .openURL(let url): return "open \(url.absoluteString)"
        case .sshRestartUnit(let host, let unit): return "ssh \(host) systemctl --user restart \(unit)"
        case .startVLLM: return "start vLLM"
        case .stopVLLM: return "stop vLLM"
        case .selectModel(let profile): return "select model \(profile.map(String.init) ?? "auto")"
        case .startLocalAgent(let label): return "launchctl start \(label)"
        case .stopLocalAgent(let label): return "launchctl stop \(label)"
        case .restartLocalAgent(let label): return "launchctl restart \(label)"
        case .sshStartUnit(let host, let unit): return "ssh \(host) systemctl --user start \(unit)"
        case .sshStopUnit(let host, let unit): return "ssh \(host) systemctl --user stop \(unit)"
        case .sshKillUnit(let host, let unit): return "ssh \(host) systemctl --user kill -s SIGKILL \(unit)"
        case .killLocalAgent(let label): return "launchctl kill SIGKILL \(label)"
        case .quitLocalApp(let name): return "pkill -x \(name)"
        }
    }
}

private extension String {
    var logLines: [String] {
        split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
            .map(String.init)
            .filter { !$0.isEmpty }
    }
}

public enum ServiceCommandError: LocalizedError {
    case urlOpenFailed(URL)
    case sshFailed(code: Int32, stderr: String)
    case launchctlFailed(code: Int32, stderr: String)
    case processKillFailed(code: Int32, stderr: String)

    public var errorDescription: String? {
        switch self {
        case .urlOpenFailed(let url):
            return "Could not open \(url.absoluteString)"
        case .sshFailed(let code, let stderr):
            return "ssh exited with code \(code): \(stderr)"
        case .launchctlFailed(let code, let stderr):
            return "launchctl exited with code \(code): \(stderr)"
        case .processKillFailed(let code, let stderr):
            return "pkill exited with code \(code): \(stderr)"
        }
    }
}

#if canImport(AppKit)
import AppKit
#endif
