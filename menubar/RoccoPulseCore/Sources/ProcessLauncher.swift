import Foundation

/// Result returned by a `ProcessLauncher` invocation. Mirrors what we need
/// from `Foundation.Process` without exposing the type itself — so tests can
/// supply a recording double.
public struct ProcessLaunchResult: Equatable, Sendable {
    public let exitCode: Int32
    public let stdout: String
    public let stderr: String

    public init(exitCode: Int32, stdout: String, stderr: String) {
        self.exitCode = exitCode
        self.stdout = stdout
        self.stderr = stderr
    }
}

/// Single seam for shelling out. Production code uses `RealProcessLauncher`;
/// tests inject `RecordingLauncher` so no real `ssh` is forked.
public protocol ProcessLauncher: AnyObject, Sendable {
    func run(executable: String, arguments: [String], timeout: TimeInterval) throws -> ProcessLaunchResult
}

public enum ProcessLauncherError: LocalizedError, Sendable {
    case executableMissing(String)
    case timeout

    public var errorDescription: String? {
        switch self {
        case .executableMissing(let path): return "Executable not found: \(path)"
        case .timeout: return "Process timed out."
        }
    }
}

/// Real `Foundation.Process` runner with a hard wall-clock timeout. Used in
/// production by `SSHProbe` and `LifecycleCommands`.
public final class RealProcessLauncher: ProcessLauncher, @unchecked Sendable {
    public init() {}

    public func run(
        executable: String,
        arguments: [String],
        timeout: TimeInterval
    ) throws -> ProcessLaunchResult {
        guard FileManager.default.fileExists(atPath: executable) else {
            throw ProcessLauncherError.executableMissing(executable)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let semaphore = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in semaphore.signal() }

        try process.run()

        if semaphore.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            // Give it 1s to exit cleanly before SIGKILL — otherwise interrupt.
            if semaphore.wait(timeout: .now() + 1) == .timedOut {
                process.interrupt()
            }
            throw ProcessLauncherError.timeout
        }

        let stdoutData = (try? stdoutPipe.fileHandleForReading.readToEnd()) ?? Data()
        let stderrData = (try? stderrPipe.fileHandleForReading.readToEnd()) ?? Data()

        return ProcessLaunchResult(
            exitCode: process.terminationStatus,
            stdout: String(data: stdoutData, encoding: .utf8) ?? "",
            stderr: String(data: stderrData, encoding: .utf8) ?? ""
        )
    }
}
