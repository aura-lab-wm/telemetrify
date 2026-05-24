import XCTest
@testable import RoccoPulseCore

/// Captures the argv handed to ssh without ever forking a real process.
final class RecordingLauncher: ProcessLauncher, @unchecked Sendable {
    var capturedExecutable: String?
    var capturedArguments: [String] = []
    var stubbedStdout: String = ""
    var stubbedExitCode: Int32 = 0
    var stubbedStderr: String = ""

    func run(
        executable: String,
        arguments: [String],
        timeout: TimeInterval
    ) throws -> ProcessLaunchResult {
        capturedExecutable = executable
        capturedArguments = arguments
        return ProcessLaunchResult(
            exitCode: stubbedExitCode,
            stdout: stubbedStdout,
            stderr: stubbedStderr
        )
    }
}

final class SSHProbeArgsTests: XCTestCase {
    func testProbeBuildsExpectedArgv() throws {
        let launcher = RecordingLauncher()
        launcher.stubbedStdout = "{\"schema_version\":1,\"host\":\"rocco\",\"ts\":0,\"agent_uptime_s\":0,\"gpus\":[],\"vllm\":{\"running\":false,\"model\":null,\"port\":8000,\"pid\":null,\"uptime_s\":0},\"services\":[],\"tier\":1,\"tier_reason\":\"x\",\"inference_recent\":null,\"errors\":[]}"

        let probe = SSHProbe(launcher: launcher)
        _ = try probe.fetchStatus()

        XCTAssertEqual(launcher.capturedExecutable, "/usr/bin/ssh")
        XCTAssertEqual(launcher.capturedArguments, [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            "-o", "ServerAliveInterval=30",
            "rocco",
            "cat",
            "~/.cache/rocco-status.json",
        ])
    }

    func testProbeReturnsParsedStatus() throws {
        let launcher = RecordingLauncher()
        let fixtureURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures")
            .appendingPathComponent("status-healthy.json")
        launcher.stubbedStdout = try String(contentsOf: fixtureURL, encoding: .utf8)

        let probe = SSHProbe(launcher: launcher)
        let status = try probe.fetchStatus()

        XCTAssertEqual(status.host, "rocco.cs.wm.edu")
        XCTAssertEqual(status.gpus.count, 2)
    }

    func testProbeMapsNonZeroExitToError() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 255
        launcher.stubbedStderr = "Permission denied (publickey)."

        let probe = SSHProbe(launcher: launcher)
        XCTAssertThrowsError(try probe.fetchStatus()) { error in
            guard case SSHProbeError.nonZeroExit(let code, _) = error else {
                XCTFail("expected SSHProbeError.nonZeroExit, got \(error)")
                return
            }
            XCTAssertEqual(code, 255)
        }
    }
}
