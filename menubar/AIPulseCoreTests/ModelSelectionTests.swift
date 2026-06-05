import XCTest
@testable import AIPulseCore

final class ModelSelectionTests: XCTestCase {

    // MARK: - LifecycleCommands.selectModel argv

    func testSelectModelPinnedArgv() throws {
        let launcher = RecordingLauncher()
        launcher.stubbedStdout = "pinned profile 2\n"
        let lifecycle = LifecycleCommands(launcher: launcher)

        try lifecycle.selectModel(2)

        XCTAssertEqual(launcher.capturedExecutable, "/usr/bin/ssh")
        XCTAssertEqual(launcher.capturedArguments, [
            "rocco",
            "cd /scratch/amastropaolo/rocco-inference && /scratch/amastropaolo/rocco-inference/.venv/bin/python -m model_manager.manager select 2 ; " +
            "cd /scratch/amastropaolo/rocco-inference && /scratch/amastropaolo/rocco-inference/.venv/bin/python -m model_manager.manager down ; sleep 1 ; " +
            "cd /scratch/amastropaolo/rocco-inference && /scratch/amastropaolo/rocco-inference/.venv/bin/python -m model_manager.manager up",
        ])
    }

    func testSelectModelAutoArgv() throws {
        let launcher = RecordingLauncher()
        launcher.stubbedStdout = "model selection: AUTO\n"
        let lifecycle = LifecycleCommands(launcher: launcher)

        try lifecycle.selectModel(nil)

        XCTAssertEqual(launcher.capturedArguments.last?.contains(
            "model_manager.manager select auto ;"), true)
    }

    func testSelectModelNonZeroExitThrows() {
        let launcher = RecordingLauncher()
        launcher.stubbedExitCode = 2
        launcher.stubbedStderr = "unknown profile 9"
        let lifecycle = LifecycleCommands(launcher: launcher)
        XCTAssertThrowsError(try lifecycle.selectModel(9)) { error in
            guard case LifecycleError.commandFailed(let code, _) = error else {
                return XCTFail("expected commandFailed, got \(error)")
            }
            XCTAssertEqual(code, 2)
        }
    }

    // MARK: - ServiceCommand → lifecycle wiring

    func testRunnerPerformsSelectModel() async throws {
        let launcher = RecordingLauncher()
        launcher.stubbedStdout = "ok\n"
        let lifecycle = LifecycleCommands(launcher: launcher)
        let runner = DefaultServiceCommandRunner(lifecycle: lifecycle)

        let summary = try await runner.perform(.selectModel(profile: 3))

        XCTAssertTrue(launcher.capturedArguments.last?.contains(
            "model_manager.manager select 3 ;") ?? false)
        XCTAssertTrue(summary.contains("3"))
    }

    func testSelectModelCommandEquatable() {
        XCTAssertEqual(ServiceCommand.selectModel(profile: 4),
                       ServiceCommand.selectModel(profile: 4))
        XCTAssertNotEqual(ServiceCommand.selectModel(profile: 4),
                          ServiceCommand.selectModel(profile: nil))
    }

    // MARK: - Status decode (schema v4 models block)

    private func decode(_ json: String) throws -> RoccoStatus {
        try RoccoStatus.decode(from: Data(json.utf8))
    }

    private func base(_ extra: String) -> String {
        """
        {"schema_version":4,"host":"rocco","ts":0,"agent_uptime_s":0,
         "gpus":[],"vllm":{"running":true,"model":"Kimi-Dev-72B","port":8000,"pid":1,"uptime_s":1},
         "services":[],"tier":4,"tier_reason":"x","inference_recent":null,"errors":[]\(extra)}
        """
    }

    func testDecodesModelsAutoSelection() throws {
        let json = base("""
        ,"models":{"selected_profile":"auto","available":[
          {"profile":4,"label":"Kimi-Dev-72B · BF16 · 4 GPU","model":"Kimi-Dev-72B","model_id":"moonshotai/Kimi-Dev-72B","precision":"bf16","gpus":4,"downloaded":true},
          {"profile":1,"label":"Qwen3-Coder-30B · FP8 · 1 GPU","model":"Qwen3-Coder-30B-A3B-Instruct","model_id":"Qwen/Qwen3-Coder-30B-A3B-Instruct","precision":"fp8","gpus":1,"downloaded":false}
        ]}
        """)
        let s = try decode(json)
        XCTAssertNil(s.models?.selectedProfile)            // "auto" → nil
        XCTAssertEqual(s.models?.available.count, 2)
        XCTAssertEqual(s.models?.available.first?.profile, 4)
        XCTAssertEqual(s.models?.available.first?.label, "Kimi-Dev-72B · BF16 · 4 GPU")
        XCTAssertEqual(s.models?.available.first?.gpus, 4)
        XCTAssertEqual(s.models?.available.last?.downloaded, false)
    }

    func testDecodesModelsPinnedSelection() throws {
        let json = base("""
        ,"models":{"selected_profile":2,"available":[]}
        """)
        let s = try decode(json)
        XCTAssertEqual(s.models?.selectedProfile, 2)       // int → pinned
    }

    func testV3SnapshotWithoutModelsStillDecodes() throws {
        // Backward-compat: a pre-v4 snapshot has no `models` key.
        let json = base("")
        let s = try decode(json)
        XCTAssertNil(s.models)
        XCTAssertEqual(s.tier, 4)
    }
}
