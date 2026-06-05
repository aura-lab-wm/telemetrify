import XCTest
@testable import AIPulseCore

/// The menubar label stops being a lone bolt: when data is FRESH it
/// carries the serving model + average GPU utilization. Stale or absent
/// snapshots show no text — a number we can't trust is worse than none.
final class MenuBarTitleTests: XCTestCase {
    private func gpu(util: Int) -> RoccoStatus.GPU {
        RoccoStatus.GPU(idx: 0, name: "NVIDIA L40S", utilPct: util,
                        memUsedMib: 500, memTotalMib: 1000, tempC: 60,
                        powerW: 0.0)
    }

    private func snapshot(running: Bool, model: String?, gpus: [RoccoStatus.GPU],
                          ageSeconds: Int = 0, now: Date = Date()) -> RoccoStatus {
        RoccoStatus(
            schemaVersion: 1,
            host: "rocco.cs.wm.edu",
            ts: Int(now.timeIntervalSince1970) - ageSeconds,
            agentUptimeS: 12345,
            gpus: gpus,
            vllm: RoccoStatus.VLLM(running: running, model: model,
                                   port: 8000, pid: 1, uptimeS: 60),
            services: [],
            tier: 4,
            tierReason: "x",
            inferenceRecent: nil,
            errors: []
        )
    }

    func testRunningShowsAbbreviatedModelAndAvgUtil() {
        let now = Date()
        let snap = snapshot(running: true, model: "Llama-3.1-WhiteRabbitNeo-2-70B",
                            gpus: [gpu(util: 98), gpu(util: 96)], now: now)
        XCTAssertEqual(MenuBarTitle.make(snapshot: snap, now: now), "WRN-2-70B 97%")
    }

    func testRunningWithoutGPUsShowsModelOnly() {
        let now = Date()
        let snap = snapshot(running: true, model: "Kimi-Dev-72B", gpus: [], now: now)
        XCTAssertEqual(MenuBarTitle.make(snapshot: snap, now: now), "Kimi-Dev-72B")
    }

    func testIdleShowsUtilOnly() {
        let now = Date()
        let snap = snapshot(running: false, model: nil,
                            gpus: [gpu(util: 4), gpu(util: 2)], now: now)
        XCTAssertEqual(MenuBarTitle.make(snapshot: snap, now: now), "3%")
    }

    func testIdleWithoutGPUsShowsNothing() {
        let now = Date()
        let snap = snapshot(running: false, model: nil, gpus: [], now: now)
        XCTAssertNil(MenuBarTitle.make(snapshot: snap, now: now))
    }

    func testStaleSnapshotShowsNothing() {
        let now = Date()
        let snap = snapshot(running: true, model: "Kimi-Dev-72B",
                            gpus: [gpu(util: 98)], ageSeconds: 3600, now: now)
        XCTAssertNil(MenuBarTitle.make(snapshot: snap, now: now))
    }

    func testNilSnapshotShowsNothing() {
        XCTAssertNil(MenuBarTitle.make(snapshot: nil, now: Date()))
    }
}
