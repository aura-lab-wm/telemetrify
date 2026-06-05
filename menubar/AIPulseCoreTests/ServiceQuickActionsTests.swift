import XCTest
@testable import AIPulseCore

/// Records EVERY launcher invocation so multi-step commands can be asserted.
private final class CallsLauncher: ProcessLauncher, @unchecked Sendable {
    var calls: [(exec: String, args: [String])] = []
    var stubbedExitCode: Int32 = 0
    var stubbedStderr: String = ""

    func run(executable: String, arguments: [String],
             timeout: TimeInterval) throws -> ProcessLaunchResult {
        calls.append((executable, arguments))
        return ProcessLaunchResult(exitCode: stubbedExitCode, stdout: "", stderr: stubbedStderr)
    }
}

/// "Every service shows a quick lifecycle icon" — runner argv, gutter
/// mapping, and registry wiring for the new start/stop/kill commands.
final class ServiceQuickActionsTests: XCTestCase {
    private let label = "com.amastropaolo.telemetrify"

    // MARK: - runner argv

    func testSSHStartUnitArgv() async throws {
        let launcher = CallsLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        let summary = try await runner.perform(
            .sshStartUnit(host: "rocco", unit: "rocco-agent.service"))

        XCTAssertEqual(launcher.calls.first?.exec, "/usr/bin/ssh")
        XCTAssertEqual(launcher.calls.first?.args, [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            "rocco",
            "systemctl", "--user", "start", "rocco-agent.service",
        ])
        XCTAssertTrue(summary.contains("rocco-agent.service"))
    }

    func testSSHStopUnitArgv() async throws {
        let launcher = CallsLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        _ = try await runner.perform(
            .sshStopUnit(host: "rocco", unit: "rocco-agent.service"))

        XCTAssertEqual(launcher.calls.first?.args.suffix(4),
                       ["systemctl", "--user", "stop", "rocco-agent.service"])
    }

    func testSSHKillUnitArgv() async throws {
        let launcher = CallsLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        _ = try await runner.perform(
            .sshKillUnit(host: "rocco", unit: "rocco-agent.service"))

        XCTAssertEqual(launcher.calls.first?.args.suffix(6),
                       ["systemctl", "--user", "kill", "-s", "SIGKILL", "rocco-agent.service"])
    }

    func testKillLocalAgentArgvTargetsGUIDomain() async throws {
        let launcher = CallsLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        _ = try await runner.perform(.killLocalAgent(label: label))

        XCTAssertEqual(launcher.calls.first?.exec, "/bin/launchctl")
        XCTAssertEqual(launcher.calls.first?.args,
                       ["kill", "SIGKILL", "gui/\(getuid())/\(label)"])
    }

    func testQuitLocalAppUsesPkill() async throws {
        let launcher = CallsLauncher()
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        let summary = try await runner.perform(.quitLocalApp(name: "Ollama"))

        XCTAssertEqual(launcher.calls.first?.exec, "/usr/bin/pkill")
        XCTAssertEqual(launcher.calls.first?.args, ["-x", "Ollama"])
        XCTAssertTrue(summary.contains("Ollama"))
    }

    func testQuitLocalAppToleratesNoMatchExitCode() async throws {
        let launcher = CallsLauncher()
        launcher.stubbedExitCode = 1   // pkill: no processes matched
        let runner = DefaultServiceCommandRunner(sshLauncher: launcher)

        let summary = try await runner.perform(.quitLocalApp(name: "Ollama"))

        XCTAssertTrue(summary.lowercased().contains("not running"))
    }

    // MARK: - gutter mapping

    func testGutterMappingForNewCommands() {
        func present(_ cmd: ServiceCommand) -> GutterPresentation {
            GutterPresentation.make(
                action: ServiceAction(label: "x", showWhen: [.up, .down, .unknown], command: cmd),
                inFlight: false)
        }
        XCTAssertEqual(present(.sshStartUnit(host: "h", unit: "u")),
                       .action(symbol: "play.fill", verb: "Start", isDestructive: false))
        XCTAssertEqual(present(.sshStopUnit(host: "h", unit: "u")),
                       .action(symbol: "stop.fill", verb: "Stop", isDestructive: true))
        XCTAssertEqual(present(.sshKillUnit(host: "h", unit: "u")),
                       .action(symbol: "xmark.octagon", verb: "Kill", isDestructive: true))
        XCTAssertEqual(present(.killLocalAgent(label: "l")),
                       .action(symbol: "xmark.octagon", verb: "Kill", isDestructive: true))
        XCTAssertEqual(present(.quitLocalApp(name: "n")),
                       .action(symbol: "stop.fill", verb: "Stop", isDestructive: true))
    }

    // MARK: - Service.actions(for:)

    func testActionsForStateReturnsAllMatchesInOrder() {
        let svc = Service(
            id: "x", displayName: "x",
            kind: .fromStatus(path: "vllm.running", label: "x"),
            actions: [
                ServiceAction(label: "Stop", showWhen: [.up], command: .stopVLLM),
                ServiceAction(label: "Restart", showWhen: [.up, .down],
                              command: .sshRestartUnit(host: "h", unit: "u"),
                              isPrimary: false),
                ServiceAction(label: "Start", showWhen: [.down], command: .startVLLM),
            ])
        XCTAssertEqual(svc.actions(for: .up).map(\.label), ["Stop", "Restart"])
        XCTAssertEqual(svc.actions(for: .down).map(\.label), ["Restart", "Start"])
        XCTAssertEqual(svc.action(for: .up)?.label, "Stop")
    }

    // MARK: - registry wiring: every built-in shows an icon in every state

    func testRoccoAgentHasQuickActionsInEveryState() {
        let svc = ServiceRegistry.builtins().first { $0.id == "rocco-agent" }!
        XCTAssertEqual(svc.action(for: .up)?.command,
                       .sshStopUnit(host: "rocco", unit: "rocco-agent.service"))
        XCTAssertEqual(svc.action(for: .down)?.command,
                       .sshStartUnit(host: "rocco", unit: "rocco-agent.service"))
        XCTAssertEqual(svc.action(for: .unknown)?.command,
                       .sshStartUnit(host: "rocco", unit: "rocco-agent.service"))
        // secondaries when up: Restart + Kill
        XCTAssertEqual(svc.actions(for: .up).map(\.label), ["Stop", "Restart", "Kill"])
    }

    func testTelemetrifyHasStopPrimaryAndKillSecondaryWhenUp() {
        let svc = ServiceRegistry.builtins().first { $0.id == "telemetrify" }!
        XCTAssertEqual(svc.action(for: .up)?.command, .stopLocalAgent(label: label))
        XCTAssertEqual(svc.actions(for: .up).map(\.label), ["Stop", "Restart", "Kill"])
        XCTAssertEqual(svc.actions(for: .up).last?.command, .killLocalAgent(label: label))
        XCTAssertEqual(svc.action(for: .down)?.command, .startLocalAgent(label: label))
    }

    func testLocalOllamaStopsViaQuitWhenUp() {
        let svc = ServiceRegistry.builtins().first { $0.id == "ollama-local" }!
        XCTAssertEqual(svc.action(for: .up)?.command, .quitLocalApp(name: "Ollama"))
        XCTAssertEqual(svc.action(for: .down)?.label, "Start")
    }

    func testPrimaryFlagBeatsDeclarationOrder() {
        // A secondary declared FIRST must not steal the gutter button.
        let svc = Service(
            id: "x", displayName: "x",
            kind: .fromStatus(path: "vllm.running", label: "x"),
            actions: [
                ServiceAction(label: "Kill", showWhen: [.up],
                              command: .killLocalAgent(label: "l"),
                              isPrimary: false),
                ServiceAction(label: "Stop", showWhen: [.up], command: .stopVLLM),
            ])
        XCTAssertEqual(svc.action(for: .up)?.label, "Stop")
        // states matching ONLY secondaries still get a button (fallback)
        let onlySecondary = Service(
            id: "y", displayName: "y",
            kind: .fromStatus(path: "vllm.running", label: "y"),
            actions: [ServiceAction(label: "Restart", showWhen: [.down],
                                    command: .sshRestartUnit(host: "h", unit: "u"),
                                    isPrimary: false)])
        XCTAssertEqual(onlySecondary.action(for: .down)?.label, "Restart")
    }
}
