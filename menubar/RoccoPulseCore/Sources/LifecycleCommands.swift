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

/// Up / down controls for the remote vLLM process. Both commands run a small
/// Python manager script that lives in `~/rocco/manager.py` on the Rocco host
/// and is the documented way for the operator to bring vLLM up or down (so
/// the menubar shells out to the same entrypoint a human would type).
public final class LifecycleCommands: @unchecked Sendable {
    public weak var delegate: LifecycleOutputDelegate?
    private let launcher: ProcessLauncher
    private let timeout: TimeInterval

    public init(launcher: ProcessLauncher = RealProcessLauncher(), timeout: TimeInterval = 30) {
        self.launcher = launcher
        self.timeout = timeout
    }

    public func startVLLM() throws {
        try run(remoteCommand: "cd ~/rocco && python manager.py up")
    }

    public func stopVLLM() throws {
        try run(remoteCommand: "cd ~/rocco && python manager.py down")
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
