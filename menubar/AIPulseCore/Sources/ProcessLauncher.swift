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

        // Drain BOTH pipes on background threads concurrently with the process.
        // Reading only after the process exits (the old behavior) deadlocks any
        // child that writes more than the OS pipe buffer (~64KB) before exiting:
        // its write blocks, so it never terminates, so we never read.
        final class IOBox: @unchecked Sendable { var out = Data(); var err = Data() }
        let box = IOBox()
        let ioGroup = DispatchGroup()
        let stdoutHandle = stdoutPipe.fileHandleForReading
        let stderrHandle = stderrPipe.fileHandleForReading
        ioGroup.enter()
        DispatchQueue.global().async {
            box.out = stdoutHandle.readDataToEndOfFile()
            ioGroup.leave()
        }
        ioGroup.enter()
        DispatchQueue.global().async {
            box.err = stderrHandle.readDataToEndOfFile()
            ioGroup.leave()
        }

        let semaphore = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in semaphore.signal() }

        try process.run()

        let timedOut = semaphore.wait(timeout: .now() + timeout) == .timedOut
        if timedOut {
            process.terminate()
            // Give it 1s to exit cleanly before SIGKILL — otherwise interrupt.
            if semaphore.wait(timeout: .now() + 1) == .timedOut {
                process.interrupt()
            }
        }

        // Wait for the readers to hit EOF (the pipe write-ends close once the
        // process and any children exit) so we never leak the IO threads.
        ioGroup.wait()

        if timedOut {
            throw ProcessLauncherError.timeout
        }

        return ProcessLaunchResult(
            exitCode: process.terminationStatus,
            stdout: String(data: box.out, encoding: .utf8) ?? "",
            stderr: String(data: box.err, encoding: .utf8) ?? ""
        )
    }
}
