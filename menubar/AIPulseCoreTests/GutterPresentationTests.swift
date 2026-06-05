import XCTest
@testable import AIPulseCore

final class GutterPresentationTests: XCTestCase {
    private func action(_ command: ServiceCommand) -> ServiceAction {
        ServiceAction(label: "x", showWhen: [.up, .down, .unknown], command: command)
    }

    func testInFlightWinsOverAnyAction() {
        let p = GutterPresentation.make(action: action(.startVLLM), inFlight: true)
        XCTAssertEqual(p, .busy)
    }

    func testNilActionIsPlaceholder() {
        XCTAssertEqual(GutterPresentation.make(action: nil, inFlight: false), .placeholder)
    }

    func testStopCommandsAreDestructiveStopIcon() {
        for cmd: ServiceCommand in [.stopVLLM, .stopLocalAgent(label: "l")] {
            let p = GutterPresentation.make(action: action(cmd), inFlight: false)
            XCTAssertEqual(p, .action(symbol: "stop.fill", verb: "Stop", isDestructive: true))
        }
    }

    func testStartCommandsArePlayIcon() {
        for cmd: ServiceCommand in [.startVLLM, .startLocalAgent(label: "l")] {
            let p = GutterPresentation.make(action: action(cmd), inFlight: false)
            XCTAssertEqual(p, .action(symbol: "play.fill", verb: "Start", isDestructive: false))
        }
    }

    func testRestartCommandsAreClockwiseIcon() {
        let cmds: [ServiceCommand] = [
            .sshRestartUnit(host: "rocco", unit: "rocco-agent.service"),
            .restartLocalAgent(label: "l"),
        ]
        for cmd in cmds {
            let p = GutterPresentation.make(action: action(cmd), inFlight: false)
            XCTAssertEqual(p, .action(symbol: "arrow.clockwise", verb: "Restart", isDestructive: false))
        }
    }

    func testOpenURLIsOutwardArrow() {
        let p = GutterPresentation.make(
            action: action(.openURL(URL(string: "http://x")!)), inFlight: false)
        XCTAssertEqual(p, .action(symbol: "arrow.up.right.square", verb: "Open", isDestructive: false))
    }

    func testSelectModelNeverRendersAGutterButton() {
        // model selection lives in the in-card picker; the gutter must
        // reserve space but show nothing actionable.
        let p = GutterPresentation.make(action: action(.selectModel(profile: 2)), inFlight: false)
        XCTAssertEqual(p, .placeholder)
    }
}
