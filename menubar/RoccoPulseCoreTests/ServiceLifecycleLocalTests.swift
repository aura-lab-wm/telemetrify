import XCTest
@testable import RoccoPulseCore

/// Records EVERY launcher invocation (the shared RecordingLauncher keeps only
/// the last), so restart — which issues stop then start — can be asserted.
private final class MultiRecordingLauncher: ProcessLauncher, @unchecked Sendable {
    var calls: [(exec: String, args: [String])] = []
    var stubbedExitCode: Int32 = 0
    var stubbedStderr: String = ""

    func run(executable: String, arguments: [String],
             timeout: TimeInterval) throws -> ProcessLaunchResult {
        calls.append((executable, arguments))
        return ProcessLaunchResult(exitCode: stubbedExitCode, stdout: "", stderr: stubbedStderr)
    }
}

final class ServiceLifecycleLocalTests: XCTestCase {
    private let label = "com.amastropaolo.telemetrify"

    func testRunnerStartLocalAgentArgv() async throws {
        let launcher = RecordingLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        let summary = try await runner.perform(.startLocalAgent(label: label))

        XCTAssertEqual(launcher.capturedExecutable, "/bin/launchctl")
        XCTAssertEqual(launcher.capturedArguments, ["start", label])
        XCTAssertTrue(summary.contains(label))
    }

    func testRunnerStopLocalAgentArgv() async throws {
        let launcher = RecordingLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        _ = try await runner.perform(.stopLocalAgent(label: label))

        XCTAssertEqual(launcher.capturedExecutable, "/bin/launchctl")
        XCTAssertEqual(launcher.capturedArguments, ["stop", label])
    }

    func testRunnerRestartIssuesStopThenStart() async throws {
        let launcher = MultiRecordingLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        _ = try await runner.perform(.restartLocalAgent(label: label))

        XCTAssertEqual(launcher.calls.map { $0.args },
                       [["stop", label], ["start", label]])
        XCTAssertTrue(launcher.calls.allSatisfy { $0.exec == "/bin/launchctl" })
    }

    func testStartLocalAgentNonZeroExitThrows() async {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 1
        launcher.stubbedStderr = "no such process"
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        do {
            _ = try await runner.perform(.startLocalAgent(label: label))
            XCTFail("expected a thrown error on non-zero launchctl exit")
        } catch let ServiceCommandError.launchctlFailed(code, _) {
            XCTAssertEqual(code, 1)
        } catch {
            XCTFail("expected ServiceCommandError.launchctlFailed, got \(error)")
        }
    }

    func testLocalAgentCommandsAreEquatable() {
        XCTAssertEqual(ServiceCommand.startLocalAgent(label: label),
                       ServiceCommand.startLocalAgent(label: label))
        XCTAssertNotEqual(ServiceCommand.startLocalAgent(label: label),
                          ServiceCommand.stopLocalAgent(label: label))
    }

    // MARK: - registry wiring

    func testTelemetrifyRowOffersStartWhenDownAndRestartWhenUp() {
        let svc = ServiceRegistry.builtins().first { $0.id == "telemetrify" }
        XCTAssertNotNil(svc)
        guard let svc else { return }

        XCTAssertEqual(svc.action(for: .down)?.command, .startLocalAgent(label: label))
        XCTAssertEqual(svc.action(for: .unknown)?.command, .startLocalAgent(label: label))
        XCTAssertEqual(svc.action(for: .up)?.command, .restartLocalAgent(label: label))
        // the dashboard URL is still carried so the row name can open it
        XCTAssertNotNil(svc.clientURL)
    }
}
