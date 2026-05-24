import Foundation

public enum SSHProbeError: LocalizedError, Sendable {
    case nonZeroExit(Int32, String)
    case decodeFailed(Error)

    public var errorDescription: String? {
        switch self {
        case .nonZeroExit(let code, let stderr):
            return "ssh exited with code \(code): \(stderr.trimmingCharacters(in: .whitespacesAndNewlines))"
        case .decodeFailed(let underlying):
            return "Could not parse rocco-status.json: \(underlying)"
        }
    }
}

/// Fetches `~/.cache/rocco-status.json` from Rocco via `ssh rocco cat …`.
/// The exact argv is locked by `SSHProbeArgsTests`; do not reorder.
///
/// We rely on the user's `~/.ssh/config` `Host rocco` entry (and its
/// `ControlMaster auto` line) so the first call sets up a multiplexed socket
/// and every subsequent call is sub-50ms. `BatchMode=yes` guarantees we never
/// hang waiting for a passphrase prompt — if the key is locked we fail fast
/// and the UI shows an "ssh-add" hint.
public final class SSHProbe: @unchecked Sendable {
    private let launcher: ProcessLauncher
    private let timeout: TimeInterval

    public init(launcher: ProcessLauncher = RealProcessLauncher(), timeout: TimeInterval = 6) {
        self.launcher = launcher
        self.timeout = timeout
    }

    public func fetchStatus() throws -> RoccoStatus {
        let args = [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            "-o", "ServerAliveInterval=30",
            "rocco",
            "cat",
            "~/.cache/rocco-status.json",
        ]
        let result = try launcher.run(
            executable: "/usr/bin/ssh",
            arguments: args,
            timeout: timeout
        )
        guard result.exitCode == 0 else {
            throw SSHProbeError.nonZeroExit(result.exitCode, result.stderr)
        }
        let data = Data(result.stdout.utf8)
        do {
            return try RoccoStatus.decode(from: data)
        } catch {
            throw SSHProbeError.decodeFailed(error)
        }
    }
}
