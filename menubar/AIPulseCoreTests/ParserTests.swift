import XCTest
@testable import AIPulseCore

final class ParserTests: XCTestCase {
    private func loadFixture(_ name: String) throws -> Data {
        let bundle = Bundle(for: type(of: self))
        guard let url = bundle.url(forResource: name, withExtension: "json") else {
            // Fall back to walking up from the source file for SPM/xcodebuild
            // setups where the resource bundle wasn't auto-wired.
            let here = URL(fileURLWithPath: #filePath)
            let candidate = here
                .deletingLastPathComponent()
                .appendingPathComponent("Fixtures")
                .appendingPathComponent("\(name).json")
            return try Data(contentsOf: candidate)
        }
        return try Data(contentsOf: url)
    }

    func testDecodesHealthyFixture() throws {
        let data = try loadFixture("status-healthy")
        let status = try RoccoStatus.decode(from: data)

        XCTAssertEqual(status.schemaVersion, 1)
        XCTAssertEqual(status.host, "rocco.cs.wm.edu")
        XCTAssertEqual(status.gpus.count, 2)
        XCTAssertEqual(status.gpus[0].idx, 0)
        XCTAssertEqual(status.gpus[0].utilPct, 42)
        XCTAssertEqual(status.gpus[0].memUsedMib, 36210)
        XCTAssertEqual(status.gpus[0].memTotalMib, 81920)
        XCTAssertEqual(status.vllm.running, true)
        XCTAssertEqual(status.vllm.model, "moonshotai/Kimi-Dev-72B")
        XCTAssertEqual(status.tier, 4)
        XCTAssertNotNil(status.inferenceRecent)
        XCTAssertEqual(status.inferenceRecent?.requestsRunning, 1)
        XCTAssertEqual(status.inferenceRecent?.tokensPerSec, 47.3)
        XCTAssertEqual(status.inferenceRecent?.isWorking, true)
    }

    func testDecodesVLLMDownFixture() throws {
        let data = try loadFixture("status-vllm-down")
        let status = try RoccoStatus.decode(from: data)

        XCTAssertEqual(status.vllm.running, false)
        XCTAssertNil(status.vllm.model)
        XCTAssertNil(status.vllm.pid)
        XCTAssertEqual(status.tier, 2)
        XCTAssertNil(status.inferenceRecent)
        XCTAssertEqual(status.errors, ["vllm port 8000 not bound"])
    }

    func testDecodesNoGPUsFixture() throws {
        let data = try loadFixture("status-no-gpus")
        let status = try RoccoStatus.decode(from: data)

        XCTAssertEqual(status.gpus.count, 0)
        XCTAssertEqual(status.tier, 1)
        XCTAssertEqual(status.tierReason, "no GPUs visible")
    }

    func testMemPctUsedComputedProperty() throws {
        let data = try loadFixture("status-healthy")
        let status = try RoccoStatus.decode(from: data)
        // GPU 0: 36210 / 81920 ≈ 44.2%
        XCTAssertEqual(status.gpus[0].memPctUsed, 44.2, accuracy: 0.2)
        // GPU 1: 1024 / 81920 = 1.25%
        XCTAssertEqual(status.gpus[1].memPctUsed, 1.25, accuracy: 0.1)
    }

    func testMemPctUsedZeroForEmptyGPU() throws {
        let data = try loadFixture("status-no-gpus")
        let status = try RoccoStatus.decode(from: data)
        XCTAssertEqual(status.gpus.count, 0)
        // No crash, no division-by-zero — just empty.
    }

    /// Regression test for the real-world shape the rocco-agent emits when
    /// vLLM isn't running: `vllm.uptime_s: null` AND `services[].pid: null`.
    /// Before this fix the decode threw, surfacing as `.decodeFailed` and
    /// the misleading "Status file malformed" popover, even though the
    /// agent was working perfectly.
    func testDecodesAgentNullsFixture() throws {
        let data = try loadFixture("status-vllm-not-running")
        let status = try RoccoStatus.decode(from: data)

        XCTAssertEqual(status.vllm.running, false)
        XCTAssertNil(status.vllm.uptimeS,
            "vllm.uptime_s null must decode to nil, not throw")
        XCTAssertEqual(status.vllm.port, 8000,
            "vllm.port stays Int — it's the configured port even when vLLM is down")
        XCTAssertEqual(status.services.count, 2)
        for svc in status.services {
            XCTAssertNil(svc.pid,
                "services[].pid null (ss can't determine owner) must decode to nil")
        }
        XCTAssertEqual(status.gpus.count, 4)
        XCTAssertEqual(status.tier, 4)
    }

    func testIsStaleAtThresholds() throws {
        let data = try loadFixture("status-healthy")
        let status = try RoccoStatus.decode(from: data)
        let baseTs = TimeInterval(status.ts)

        // ts=now-30  → not stale (≤ 60s)
        XCTAssertFalse(status.isStale(now: Date(timeIntervalSince1970: baseTs + 30)))
        // ts=now-90  → stale (> 60s)
        XCTAssertTrue(status.isStale(now: Date(timeIntervalSince1970: baseTs + 90)))
        // ts=now-3600 → still stale
        XCTAssertTrue(status.isStale(now: Date(timeIntervalSince1970: baseTs + 3600)))
    }
}

extension ParserTests {
    /// The popover's whole "name the model that's about to load" UX hinges
    /// on this field surviving Codable. Pin it so a future refactor of
    /// VLLM's CodingKeys can't silently drop it.
    func testVLLMConfiguredModelDecodesWhenOffline() throws {
        let data = try loadFixture("status-vllm-not-running")
        let status = try RoccoStatus.decode(from: data)
        XCTAssertFalse(status.vllm.running)
        XCTAssertNil(status.vllm.model,
            "model is null when vllm isn't running")
        XCTAssertEqual(status.vllm.configuredModel, "Kimi-Dev-72B",
            "configured_model carries 'what WOULD load' for the popover hint")
    }

    func testVLLMConfiguredModelIsOptionalWhenAbsent() throws {
        // The healthy fixture predates the configured_model field. Decoding
        // must not throw — it's optional on the wire.
        let data = try loadFixture("status-healthy")
        let status = try RoccoStatus.decode(from: data)
        XCTAssertTrue(status.vllm.running)
        XCTAssertNil(status.vllm.configuredModel,
            "absent configured_model in JSON → nil on the model, no decode error")
    }
}
