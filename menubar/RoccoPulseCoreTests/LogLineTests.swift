import XCTest
@testable import RoccoPulseCore

final class LogLineTests: XCTestCase {
    func testParseAssignsStableIDsInOrder() {
        let lines = LogLine.parse("a\nb\nc")
        XCTAssertEqual(lines.map(\.id), [0, 1, 2])
        XCTAssertEqual(lines.map(\.text), ["a", "b", "c"])
    }

    func testParseSkipsEmptyLines() {
        XCTAssertEqual(LogLine.parse("a\n\n\nb").map(\.text), ["a", "b"])
    }

    func testLevelDetection() {
        XCTAssertEqual(LogLine.parse("ERROR: CUDA out of memory").first?.level, .error)
        XCTAssertEqual(LogLine.parse("[2026-06-05] WARNING kv-cache nearly full").first?.level, .warn)
        XCTAssertEqual(LogLine.parse("INFO:     Started server process").first?.level, .info)
        XCTAssertEqual(LogLine.parse("Traceback (most recent call last):").first?.level, .error)
        XCTAssertEqual(LogLine.parse("model load failed after 3 retries").first?.level, .error)
        // Plain lines have no level — they're untagged, not info.
        XCTAssertNil(LogLine.parse("just some output").first?.level)
    }

    func testFilterByLevelKeepsUntaggedLines() {
        let lines = LogLine.parse("INFO ok\nERROR boom\nplain")
        var f = LogFilter()
        f.enabledLevels = [.error]
        // untagged lines always pass the level filter; only tagged
        // lines of a DISABLED level are hidden.
        XCTAssertEqual(f.apply(to: lines).map(\.text), ["ERROR boom", "plain"])
    }

    func testFilterByQueryIsCaseInsensitive() {
        let lines = LogLine.parse("loading WhiteRabbitNeo\nGET /health 200")
        var f = LogFilter()
        f.query = "whiterabbit"
        XCTAssertEqual(f.apply(to: lines).map(\.text), ["loading WhiteRabbitNeo"])
    }

    func testDefaultFilterPassesEverything() {
        let lines = LogLine.parse("INFO a\nWARN b\nERROR c\nplain")
        XCTAssertEqual(LogFilter().apply(to: lines).count, 4)
    }
}
