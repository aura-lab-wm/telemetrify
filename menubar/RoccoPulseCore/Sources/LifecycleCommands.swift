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
        // `manager.py up` is IDEMPOTENT: if the manager daemon is already
        // running it just prints "Already running, pid N" and exits 0 —
        // which means clicking Start when the manager is alive-but-stuck
        // (tier > 0 and vllm not running, manager loop hung or in a bad
        // restart-backoff window) was a silent no-op. The user
        // legitimately complained the UI "acts very dumb" because of it.
        //
        // Recycle the manager: `down` (kills any vllm + the manager loop)
        // → `up` (spawns a fresh manager that re-evaluates state and
        // starts vllm on its very first tick if tier > 0).
        //
        // Safe to run even when vllm is already up — `down` will stop it
        // cleanly first, then up re-starts it via the manager. The user
        // only sees Start when vllm is OFFLINE so that case is rare.
        let cd = "cd \(Self.remoteProjectPath)"
        let py = Self.remoteVenvPython
        try run(remoteCommand:
            "\(cd) && \(py) -m model_manager.manager down ; " +
            "sleep 1 ; " +
            "\(cd) && \(py) -m model_manager.manager up")
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

    /// Pin a model profile (1...4) or `nil` for auto, then recycle the
    /// manager so it relaunches vLLM with the new selection. `select`
    /// only writes the override file; the `down ; up` is what actually
    /// re-evaluates and (re)starts vLLM under the chosen config.
    public func selectModel(_ profile: Int?) throws {
        // `profile` is an Int or nil → "auto"; no untrusted text reaches
        // the remote shell.
        let target = profile.map(String.init) ?? "auto"
        let cd = "cd \(Self.remoteProjectPath)"
        let py = Self.remoteVenvPython
        try run(remoteCommand:
            "\(cd) && \(py) -m model_manager.manager select \(target) ; " +
            "\(cd) && \(py) -m model_manager.manager down ; sleep 1 ; " +
            "\(cd) && \(py) -m model_manager.manager up")
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
