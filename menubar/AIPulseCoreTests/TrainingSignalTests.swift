import XCTest
@testable import AIPulseCore

final class TrainingSignalTests: XCTestCase {

    private func decode(_ json: String) throws -> RoccoStatus {
        try RoccoStatus.decode(from: Data(json.utf8))
    }

    /// schema v5 envelope with overridable models + training tails.
    private func snap(tier: Int, selected: String, training: String) -> String {
        """
        {"schema_version":5,"host":"rocco","ts":0,"agent_uptime_s":0,
         "gpus":[],"vllm":{"running":true,"model":"x","port":8000,"pid":1,"uptime_s":1},
         "services":[],"tier":\(tier),"tier_reason":"x","inference_recent":null,"errors":[],
         "models":{"selected_profile":\(selected),"available":[]},
         "training":\(training)}
        """
    }

    func testDecodesTrainingBlock() throws {
        let s = try decode(snap(tier: 2, selected: "\"auto\"", training: """
        {"source":"aura-pulse","available":true,"running":true,"jobs":[
          {"pid":4242,"cmdline":"accelerate launch axolotl","owner":"amastropaolo","started_at":1779700000}
        ]}
        """))
        XCTAssertEqual(s.training?.source, "aura-pulse")
        XCTAssertEqual(s.training?.available, true)
        XCTAssertEqual(s.training?.running, true)
        XCTAssertEqual(s.training?.jobs.count, 1)
        XCTAssertEqual(s.training?.jobs.first?.pid, 4242)
        XCTAssertEqual(s.training?.jobs.first?.owner, "amastropaolo")
        XCTAssertEqual(s.training?.jobs.first?.startedAt, 1779700000)
    }

    func testTrainingUnreachableDecodes() throws {
        let s = try decode(snap(tier: 4, selected: "\"auto\"", training: """
        {"source":"aura-pulse","available":false,"running":false,"jobs":[]}
        """))
        XCTAssertEqual(s.training?.available, false)
        XCTAssertEqual(s.training?.running, false)
        XCTAssertEqual(s.training?.jobs, [])
    }

    func testV4SnapshotWithoutTrainingDecodes() throws {
        // Pre-v5 snapshot has no `training` key.
        let json = """
        {"schema_version":4,"host":"r","ts":0,"agent_uptime_s":0,"gpus":[],
         "vllm":{"running":false,"model":null,"port":8000,"pid":null,"uptime_s":0},
         "services":[],"tier":4,"tier_reason":"x","inference_recent":null,"errors":[]}
        """
        let s = try decode(json)
        XCTAssertNil(s.training)
        XCTAssertFalse(s.isAutoCappedByTraining)
    }

    // MARK: - capped-by-training heuristic

    private let runningJob = """
    {"source":"aura-pulse","available":true,"running":true,"jobs":[{"pid":1,"cmdline":"train","owner":"amastropaolo","started_at":0}]}
    """
    private let idle = """
    {"source":"aura-pulse","available":true,"running":false,"jobs":[]}
    """

    func testCappedWhenAutoBelowTopTierAndTraining() throws {
        let s = try decode(snap(tier: 2, selected: "\"auto\"", training: runningJob))
        XCTAssertTrue(s.isAutoCappedByTraining)
    }

    func testNotCappedWhenPinned() throws {
        // A deliberate pin isn't "capped" — the user chose it.
        let s = try decode(snap(tier: 2, selected: "2", training: runningJob))
        XCTAssertFalse(s.isAutoCappedByTraining)
    }

    func testNotCappedAtTopTier() throws {
        // Auto already on the biggest model → training isn't capping anything.
        let s = try decode(snap(tier: 4, selected: "\"auto\"", training: runningJob))
        XCTAssertFalse(s.isAutoCappedByTraining)
    }

    func testNotCappedWhenNoTraining() throws {
        let s = try decode(snap(tier: 2, selected: "\"auto\"", training: idle))
        XCTAssertFalse(s.isAutoCappedByTraining)
    }
}
