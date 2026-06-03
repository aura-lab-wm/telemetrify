import XCTest
@testable import RoccoPulseCore

final class ProcessLauncherTests: XCTestCase {

    /// Regression: the launcher used to read both pipes only AFTER the process
    /// exited. A child that writes more than the OS pipe buffer (~64KB) before
    /// exiting then blocks on write forever → the process never terminates →
    /// the launcher hits its timeout. `/usr/bin/seq 1 100000` emits ~580KB, so
    /// under the old code this test would hang until the timeout and fail; with
    /// concurrent draining it completes immediately with the full output.
    func testDrainsLargeStdoutWithoutDeadlock() throws {
        let launcher = RealProcessLauncher()
        let result = try launcher.run(
            executable: "/usr/bin/seq",
            arguments: ["1", "100000"],
            timeout: 15)

        XCTAssertEqual(result.exitCode, 0)
        XCTAssertGreaterThan(result.stdout.count, 500_000,
                             "full large stdout must be drained, not truncated")
        XCTAssertTrue(result.stdout.contains("100000"))
    }

    func testCapturesExitCodeAndStdout() throws {
        let launcher = RealProcessLauncher()
        let result = try launcher.run(
            executable: "/bin/echo", arguments: ["hello"], timeout: 5)
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(result.stdout.trimmingCharacters(in: .whitespacesAndNewlines), "hello")
    }

    func testMissingExecutableThrows() {
        let launcher = RealProcessLauncher()
        XCTAssertThrowsError(
            try launcher.run(executable: "/nope/does/not/exist",
                             arguments: [], timeout: 5))
    }
}
