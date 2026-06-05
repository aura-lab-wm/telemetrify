import XCTest
@testable import AIPulseCore

final class StaleStatusTests: XCTestCase {
    private func makeStatus(tsOffset: TimeInterval, now: Date) -> RoccoStatus {
        let ts = Int(now.timeIntervalSince1970 + tsOffset)
        return RoccoStatus(
            schemaVersion: 1,
            host: "rocco.cs.wm.edu",
            ts: ts,
            agentUptimeS: 60,
            gpus: [],
            vllm: .init(running: false, model: nil, port: 8000, pid: nil, uptimeS: 0),
            services: [],
            tier: 3,
            tierReason: "test",
            inferenceRecent: nil,
            errors: []
        )
    }

    func testFreshAt30s() {
        let now = Date()
        let status = makeStatus(tsOffset: -30, now: now)
        XCTAssertEqual(IconState.derive(snapshot: status, lastError: nil, now: now), .fresh)
        XCTAssertFalse(status.isStale(now: now))
    }

    func testStaleAt90s() {
        let now = Date()
        let status = makeStatus(tsOffset: -90, now: now)
        XCTAssertEqual(IconState.derive(snapshot: status, lastError: nil, now: now), .stale)
        XCTAssertTrue(status.isStale(now: now))
    }

    func testVeryStaleAt3600s() {
        let now = Date()
        let status = makeStatus(tsOffset: -3600, now: now)
        XCTAssertEqual(IconState.derive(snapshot: status, lastError: nil, now: now), .veryStale)
        XCTAssertTrue(status.isStale(now: now))
    }

    func testIconStateUnreachableWhenLastErrorPresent() {
        let now = Date()
        XCTAssertEqual(
            IconState.derive(snapshot: nil, lastError: "ssh: connection refused", now: now),
            .unreachable
        )
    }

    func testIconStateUnknownWhenNoSnapshotNoError() {
        let now = Date()
        XCTAssertEqual(IconState.derive(snapshot: nil, lastError: nil, now: now), .unknown)
    }
}
