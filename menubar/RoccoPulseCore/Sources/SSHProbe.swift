import Foundation

/// Coarse classification of "why did this probe fail?" — drives the
/// IconState + the popover's hint copy. Add a new case here when there's a
/// new failure mode the UI should react to differently.
public enum SSHProbeErrorKind: Equatable, Sendable {
    /// SSH connected but `cat ~/.cache/rocco-status.json` returned
    /// "No such file or directory" — the rocco-agent hasn't been deployed
    /// on the remote, OR is installed but hasn't written its first sample
    /// yet. Either way, the actionable hint is "run install.sh".
    case agentFileMissing
    /// SSH itself failed (network, auth, host key, …) or returned a
    /// non-zero exit code for any other reason.
    case sshFailed
    /// SSH succeeded, file read, but the JSON didn't parse.
    case decodeFailed
    /// Failure path we can't classify: ProcessLauncher couldn't even spawn
    /// `ssh` (binary missing, sandbox denial, timeout) or some other
    /// non-SSHProbeError leaked through. Surfaces a generic diagnosis
    /// instead of the misleading "try ssh-add" hint.
    case unknown
}

public enum SSHProbeError: LocalizedError, Sendable {
    case nonZeroExit(Int32, String)
    case agentFileMissing(String)        // stderr captured for diagnostics
    case decodeFailed(Error)

    public var kind: SSHProbeErrorKind {
        switch self {
        case .nonZeroExit:        return .sshFailed
        case .agentFileMissing:   return .agentFileMissing
        case .decodeFailed:       return .decodeFailed
        }
    }

    public var errorDescription: String? {
        switch self {
        case .nonZeroExit(let code, let stderr):
            return "ssh exited with code \(code): \(stderr.trimmingCharacters(in: .whitespacesAndNewlines))"
        case .agentFileMissing(let stderr):
            let trimmed = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty
                ? "rocco-agent not installed on remote (run install.sh)"
                : "rocco-agent not installed on remote (run install.sh) — \(trimmed)"
        case .decodeFailed(let underlying):
            return "Could not parse rocco-status.json: \(underlying)"
        }
    }

    /// The captured stderr for the agent-missing case (empty for other
    /// cases). Lets the UI render the raw remote output verbatim so the
    /// operator can see e.g. a wrong-path / permission-denied / NFS error
    /// instead of being told a canned "run install.sh" that won't fix it.
    public var capturedStderr: String {
        switch self {
        case .agentFileMissing(let s): return s
        case .nonZeroExit(_, let s):   return s
        case .decodeFailed:            return ""
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
public class SSHProbe: @unchecked Sendable {
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
            if Self.looksLikeAgentFileMissing(stderr: result.stderr,
                                              exitCode: result.exitCode) {
                throw SSHProbeError.agentFileMissing(result.stderr)
            }
            throw SSHProbeError.nonZeroExit(result.exitCode, result.stderr)
        }
        let data = Data(result.stdout.utf8)
        do {
            return try RoccoStatus.decode(from: data)
        } catch {
            throw SSHProbeError.decodeFailed(error)
        }
    }

    /// Classify "agent file missing" by anchoring on the canonical
    /// `cat: <path>: <reason>` shape that GNU/BSD `cat` always emits when
    /// reporting an ENOENT for the requested file. Two constraints:
    ///
    /// 1. We require the stderr line to literally START with `cat: ` and
    ///    contain `rocco-status.json` BEFORE the second colon — this rules
    ///    out false positives from ssh wrappers / ForceCommand prologues
    ///    that merely echo the command path into stderr.
    /// 2. We accept exit codes {1, 2} (GNU coreutils returns 1; some
    ///    restricted shells / busybox builds bubble up 2 for the same
    ///    condition), and we deliberately do NOT match on the localized
    ///    reason text — that's what `LANG=fr_FR.UTF-8` over SSH
    ///    `AcceptEnv LANG` would break. The `cat:` prefix and the path are
    ///    both invariant across locales.
    static func looksLikeAgentFileMissing(stderr: String, exitCode: Int32) -> Bool {
        guard exitCode == 1 || exitCode == 2 else { return false }
        // First non-empty stderr line — wrappers sometimes prepend a
        // banner; we only care that A line matches the cat: pattern, but
        // we still anchor on the *start* of that line.
        for raw in stderr.split(whereSeparator: \.isNewline) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard line.hasPrefix("cat: ") else { continue }
            // Split on ":" — pattern is "cat: <path>: <localized reason>".
            // We require at least 3 parts and `rocco-status.json` in the path.
            let parts = line.split(separator: ":", maxSplits: 2,
                                   omittingEmptySubsequences: false)
            guard parts.count >= 3 else { continue }
            let pathPart = parts[1].trimmingCharacters(in: .whitespaces)
            if pathPart.hasSuffix("rocco-status.json") {
                return true
            }
        }
        return false
    }
}
