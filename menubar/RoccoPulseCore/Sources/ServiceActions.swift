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

    public func perform(_ command: ServiceCommand) async throws -> String {
        switch command {
        case .openURL(let url):
            if urlOpener(url) {
                return "Opened \(url.absoluteString)"
            }
            throw ServiceCommandError.urlOpenFailed(url)

        case .sshRestartUnit(let host, let unit):
            let args = [
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=4",
                host,
                "systemctl", "--user", "restart", unit,
            ]
            let result = try sshLauncher.run(
                executable: "/usr/bin/ssh",
                arguments: args,
                timeout: 8)
            guard result.exitCode == 0 else {
                throw ServiceCommandError.sshFailed(
                    code: result.exitCode,
                    stderr: result.stderr.trimmingCharacters(in: .whitespacesAndNewlines))
            }
            return "\(unit) restarted on \(host)"

        case .startVLLM:
            try lifecycle.startVLLM()
            return "vLLM start requested"

        case .stopVLLM:
            try lifecycle.stopVLLM()
            return "vLLM stop requested"

        case .selectModel(let profile):
            try lifecycle.selectModel(profile)
            let name = profile.map { "profile \($0)" } ?? "Auto"
            return "Model: \(name) — restarting vLLM"

        case .startLocalAgent(let label):
            try runLaunchctl(["start", label])
            return "\(label) started"

        case .stopLocalAgent(let label):
            try runLaunchctl(["stop", label])
            return "\(label) stopped"

        case .restartLocalAgent(let label):
            // Stop is best-effort: the agent may already be stopped, and the
            // operation that must succeed is the start. Mirrors bin/service.
            _ = try? runLaunchctl(["stop", label])
            try runLaunchctl(["start", label])
            return "\(label) restarted"
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
        guard result.exitCode == 0 else {
            throw ServiceCommandError.launchctlFailed(
                code: result.exitCode,
                stderr: result.stderr.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return result
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

public enum ServiceCommandError: LocalizedError {
    case urlOpenFailed(URL)
    case sshFailed(code: Int32, stderr: String)
    case launchctlFailed(code: Int32, stderr: String)

    public var errorDescription: String? {
        switch self {
        case .urlOpenFailed(let url):
            return "Could not open \(url.absoluteString)"
        case .sshFailed(let code, let stderr):
            return "ssh exited with code \(code): \(stderr)"
        case .launchctlFailed(let code, let stderr):
            return "launchctl exited with code \(code): \(stderr)"
        }
    }
}

#if canImport(AppKit)
import AppKit
#endif
