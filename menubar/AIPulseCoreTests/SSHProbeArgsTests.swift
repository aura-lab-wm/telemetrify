import XCTest
@testable import AIPulseCore

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

    /// `cat` exits 1 + stderr containing "rocco-status.json" + "No such file"
    /// means SSH succeeded but the rocco-agent file is missing on the remote.
    /// That's a fundamentally different state than "ssh down" — surface it
    /// distinctly so the popover can show an install hint instead of a
    /// generic unreachable error.
    func testProbeRecognizesAgentFileMissing() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 1
        launcher.stubbedStderr =
            "cat: /home/amastropaolo/.cache/rocco-status.json: No such file or directory"

        let probe = SSHProbe(launcher: launcher)
        XCTAssertThrowsError(try probe.fetchStatus()) { error in
            guard case SSHProbeError.agentFileMissing = error else {
                XCTFail("expected SSHProbeError.agentFileMissing, got \(error)")
                return
            }
        }
    }

    /// An auth / connectivity failure must NOT be reclassified as
    /// agent-missing even if stderr is short — guard against over-broad
    /// pattern matching.
    func testProbeKeepsAuthFailureAsNonZeroExit() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 255
        launcher.stubbedStderr = "ssh: connect to host rocco port 22: Network is unreachable"

        let probe = SSHProbe(launcher: launcher)
        XCTAssertThrowsError(try probe.fetchStatus()) { error in
            guard case SSHProbeError.nonZeroExit = error else {
                XCTFail("expected nonZeroExit, got \(error)")
                return
            }
        }
    }

    /// Localized stderr (LANG forwarded over SSH) must still classify as
    /// agent-missing — we anchor on the `cat: <path>:` shape, not the
    /// English reason text.
    func testProbeRecognizesAgentFileMissingUnderForeignLocale() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 1
        launcher.stubbedStderr =
            "cat: /home/amastropaolo/.cache/rocco-status.json: Aucun fichier ou dossier de ce type"

        let probe = SSHProbe(launcher: launcher)
        XCTAssertThrowsError(try probe.fetchStatus()) { error in
            guard case SSHProbeError.agentFileMissing = error else {
                XCTFail("expected agentFileMissing under fr_FR, got \(error)")
                return
            }
        }
    }

    /// Restricted shells / busybox sometimes bubble ENOENT as exit 2;
    /// classify those as agent-missing too.
    func testProbeRecognizesAgentFileMissingExit2() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 2
        launcher.stubbedStderr =
            "cat: /home/amastropaolo/.cache/rocco-status.json: No such file or directory"

        let probe = SSHProbe(launcher: launcher)
        XCTAssertThrowsError(try probe.fetchStatus()) { error in
            guard case SSHProbeError.agentFileMissing = error else {
                XCTFail("expected agentFileMissing for exit 2, got \(error)")
                return
            }
        }
    }

    /// A wrapper that ECHOES the command path into stderr (e.g. ForceCommand
    /// banner) must NOT trip the agent-missing classifier — we require the
    /// `cat: ` prefix, not just substring presence.
    func testProbeDoesNotMisclassifyWrapperEcho() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 1
        launcher.stubbedStderr = """
        [wrapper] attempted: cat /home/x/.cache/rocco-status.json
        bash: rocco: command not found
        """
        let probe = SSHProbe(launcher: launcher)
        XCTAssertThrowsError(try probe.fetchStatus()) { error in
            guard case SSHProbeError.nonZeroExit = error else {
                XCTFail("wrapper echo must stay nonZeroExit, got \(error)")
                return
            }
        }
    }

    /// `errorDescription` for `.agentFileMissing` must include the captured
    /// stderr when it's non-empty so the operator can see the real path
    /// instead of the canned hint when something else went wrong.
    func testAgentFileMissingErrorDescriptionSurfacesStderr() {
        let err = SSHProbeError.agentFileMissing(
            "cat: /home/other/.cache/rocco-status.json: Permission denied")
        XCTAssertTrue(err.errorDescription!
            .contains("/home/other/.cache/rocco-status.json"),
            "expected stderr to be surfaced, got \(err.errorDescription ?? "nil")")
        XCTAssertEqual(err.kind, .agentFileMissing)
        XCTAssertEqual(err.capturedStderr,
            "cat: /home/other/.cache/rocco-status.json: Permission denied")
    }
}



/// Lightweight in-test fixture so we don't need to touch the JSON file just
/// to build a fresh `RoccoStatus` for `IconState` tests.
enum RoccoStatusFixtures {
    static func healthy(now: Date) -> RoccoStatus {
        RoccoStatus(
            schemaVersion: 1,
            host: "rocco.cs.wm.edu",
            ts: Int(now.timeIntervalSince1970),
            agentUptimeS: 12345,
            gpus: [],
            vllm: RoccoStatus.VLLM(running: true, model: "Kimi-Dev-72B",
                                   port: 8000, pid: 1234, uptimeS: 7200),
            services: [],
            tier: 4,
            tierReason: "4 GPUs free",
            inferenceRecent: nil,
            errors: []
        )
    }
}
