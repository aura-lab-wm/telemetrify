import Foundation

public enum LifecycleError: LocalizedError, Sendable {
    case commandFailed(Int32, String)

    public var errorDescription: String? {
        switch self {
        case .commandFailed(let code, let stderr):
            return "Lifecycle command failed (exit \(code)): \(stderr.trimmingCharacters(in: .whitespacesAndNewlines))"
        }
    }
}

public protocol LifecycleOutputDelegate: AnyObject, Sendable {
    func lifecycle(_ commands: LifecycleCommands, didEmit line: String)
}

/// Up / down controls for the remote vLLM process. Both commands invoke the
/// `model_manager.manager` Python module that lives under
/// `/scratch/amastropaolo/rocco-inference/` on the Rocco host. The module is
/// the documented entrypoint a human would type, exposing `up`, `down`, and
/// `status` subcommands; `up` daemonizes via a double-fork and returns
/// immediately so the SSH command does not block on the poll loop.
public final class LifecycleCommands: @unchecked Sendable {
    /// Absolute path to the rocco-inference project root on the remote host.
    /// Hardcoded by design (single user, single host); update here if the host
    /// layout ever moves.
    static let remoteProjectPath = "/scratch/amastropaolo/rocco-inference"
    /// Absolute path to the project's venv Python on the remote host. The
    /// remote login shell exposes only `/usr/bin/python3`, which lacks the
    /// vLLM and gpu_monitor dependencies; the venv has them.
    static let remoteVenvPython = "/scratch/amastropaolo/rocco-inference/.venv/bin/python"

    public weak var delegate: LifecycleOutputDelegate?
    private let launcher: ProcessLauncher
    private let timeout: TimeInterval

    public init(launcher: ProcessLauncher = RealProcessLauncher(), timeout: TimeInterval = 30) {
        self.launcher = launcher
        self.timeout = timeout
    }

    public func startVLLM() throws {
        try run(remoteCommand: "cd \(Self.remoteProjectPath) && \(Self.remoteVenvPython) -m model_manager.manager up")
    }

    public func stopVLLM() throws {
        try run(remoteCommand: "cd \(Self.remoteProjectPath) && \(Self.remoteVenvPython) -m model_manager.manager down")
    }

    private func run(remoteCommand: String) throws {
        let result = try launcher.run(
            executable: "/usr/bin/ssh",
            arguments: ["rocco", remoteCommand],
            timeout: timeout
        )
        forward(stdout: result.stdout)
        guard result.exitCode == 0 else {
            throw LifecycleError.commandFailed(result.exitCode, result.stderr)
        }
    }

    private func forward(stdout: String) {
        guard let delegate else { return }
        for rawLine in stdout.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
            let line = String(rawLine)
            guard !line.isEmpty else { continue }
            delegate.lifecycle(self, didEmit: line)
        }
    }
}
