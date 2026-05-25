import XCTest
@testable import RoccoPulseCore

final class CollectingDelegate: LifecycleOutputDelegate, @unchecked Sendable {
    var lines: [String] = []
    func lifecycle(_ commands: LifecycleCommands, didEmit line: String) {
        lines.append(line)
    }
}

final class LifecycleCommandTests: XCTestCase {
    func testStartVLLMArgvMatchesPlan() throws {
        let launcher = RecordingLauncher()
        launcher.stubbedStdout = "starting vllm\nport 8000 bound\n"
        let delegate = CollectingDelegate()

        let lifecycle = LifecycleCommands(launcher: launcher)
        lifecycle.delegate = delegate
        try lifecycle.startVLLM()

        XCTAssertEqual(launcher.capturedExecutable, "/usr/bin/ssh")
        XCTAssertEqual(launcher.capturedArguments, [
            "rocco",
            "cd /scratch/amastropaolo/rocco-inference && /scratch/amastropaolo/rocco-inference/.venv/bin/python -m model_manager.manager up",
        ])
        XCTAssertEqual(delegate.lines, ["starting vllm", "port 8000 bound"])
    }

    func testStopVLLMArgvMatchesPlan() throws {
        let launcher = RecordingLauncher()
        launcher.stubbedStdout = "stopping vllm\nbye\n"
        let delegate = CollectingDelegate()

        let lifecycle = LifecycleCommands(launcher: launcher)
        lifecycle.delegate = delegate
        try lifecycle.stopVLLM()

        XCTAssertEqual(launcher.capturedExecutable, "/usr/bin/ssh")
        XCTAssertEqual(launcher.capturedArguments, [
            "rocco",
            "cd /scratch/amastropaolo/rocco-inference && /scratch/amastropaolo/rocco-inference/.venv/bin/python -m model_manager.manager down",
        ])
        XCTAssertEqual(delegate.lines, ["stopping vllm", "bye"])
    }

    func testNonZeroExitThrows() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 1
        launcher.stubbedStderr = "manager.py: missing config"

        let lifecycle = LifecycleCommands(launcher: launcher)
        XCTAssertThrowsError(try lifecycle.startVLLM()) { error in
            guard case LifecycleError.commandFailed(let code, _) = error else {
                XCTFail("expected LifecycleError.commandFailed, got \(error)")
                return
            }
            XCTAssertEqual(code, 1)
        }
    }
}
