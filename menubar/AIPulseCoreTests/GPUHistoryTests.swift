import XCTest
@testable import AIPulseCore

final class GPUHistoryTests: XCTestCase {
    private func gpu(idx: Int, util: Int = 50, usedMib: Int = 500,
                     totalMib: Int = 1000, temp: Int = 60) -> RoccoStatus.GPU {
        // RoccoStatus.GPU has no explicit public init; the compiler-synthesised
        // memberwise init is internal, reachable via @testable import.
        // powerW is present in the real struct (schema field power_w).
        RoccoStatus.GPU(idx: idx, name: "NVIDIA L40S", utilPct: util,
                        memUsedMib: usedMib, memTotalMib: totalMib,
                        tempC: temp, powerW: 0.0)
    }

    func testAppendRecordsMemFractionPerGPU() {
        var h = GPUHistory(capacity: 4)
        h.append(gpus: [gpu(idx: 0, usedMib: 250, totalMib: 1000),
                        gpu(idx: 1, usedMib: 750, totalMib: 1000)])
        XCTAssertEqual(h.samples(for: 0), [0.25])
        XCTAssertEqual(h.samples(for: 1), [0.75])
    }

    func testCapacityEvictsOldestFirst() {
        var h = GPUHistory(capacity: 3)
        for used in [100, 200, 300, 400] {
            h.append(gpus: [gpu(idx: 0, usedMib: used, totalMib: 1000)])
        }
        XCTAssertEqual(h.samples(for: 0), [0.2, 0.3, 0.4])
    }

    func testUnknownGPUReturnsEmpty() {
        XCTAssertEqual(GPUHistory(capacity: 3).samples(for: 9), [])
    }

    func testSummaryAveragesAndMax() {
        let s = GPUSummary(gpus: [
            gpu(idx: 0, util: 98, usedMib: 900, totalMib: 1000, temp: 69),
            gpu(idx: 1, util: 98, usedMib: 880, totalMib: 1000, temp: 70),
            gpu(idx: 2, util: 96, usedMib: 880, totalMib: 1000, temp: 64),
            gpu(idx: 3, util: 96, usedMib: 880, totalMib: 1000, temp: 57),
        ])
        XCTAssertEqual(s?.avgUtilPct, 97)
        XCTAssertEqual(s?.avgMemPct, 89)   // (90+88+88+88)/4 = 88.5 → rounds to 89
        XCTAssertEqual(s?.maxTempC, 70)
    }

    func testSummaryNilForEmpty() {
        XCTAssertNil(GPUSummary(gpus: []))
    }
}
