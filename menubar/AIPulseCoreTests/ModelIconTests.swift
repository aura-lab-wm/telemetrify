import XCTest
@testable import AIPulseCore

/// The model picker menu shows each model's ORIGINAL vendor avatar
/// (fetched from the HF org) before the name. Mapping is name-based —
/// the agent reports bare model names with no org prefix.
final class ModelIconTests: XCTestCase {
    func testVendorFamilies() {
        XCTAssertEqual(ModelIcon.asset(for: "Kimi-Dev-72B"), "model-moonshotai")
        XCTAssertEqual(ModelIcon.asset(for: "Qwen2.5-Coder-32B-Instruct"), "model-qwen")
        XCTAssertEqual(ModelIcon.asset(for: "Qwen3-VL-32B-Instruct"), "model-qwen")
        XCTAssertEqual(ModelIcon.asset(for: "QwQ-32B"), "model-qwen")
        XCTAssertEqual(ModelIcon.asset(for: "GLM-4-32B-0414"), "model-zai")
        XCTAssertEqual(ModelIcon.asset(for: "DeepSeek-Coder-V2-Lite-Instruct"), "model-deepseek")
        XCTAssertEqual(ModelIcon.asset(for: "Mistral-Small-3.2-24B-Instruct-2506"), "model-mistral")
        XCTAssertEqual(ModelIcon.asset(for: "Llama-3.3-70B-Instruct-FP8-dynamic"), "model-meta")
        XCTAssertEqual(ModelIcon.asset(for: "InternVL3-38B"), "model-opengvlab")
        XCTAssertEqual(ModelIcon.asset(for: "dots.ocr"), "model-rednote")
        XCTAssertEqual(ModelIcon.asset(for: "WhiteRabbitNeo-33B-v1.5"), "model-whiterabbitneo")
    }

    func testWhiteRabbitNeoFinetuneWinsOverLlamaBase() {
        // "Llama-3.1-WhiteRabbitNeo-2-70B" is a WRN finetune of a Llama
        // base — the finetune vendor is the identity the operator picks.
        XCTAssertEqual(ModelIcon.asset(for: "Llama-3.1-WhiteRabbitNeo-2-70B"),
                       "model-whiterabbitneo")
    }

    func testUnknownModelHasNoAsset() {
        XCTAssertNil(ModelIcon.asset(for: "TotallyNovel-9B"))
    }

    func testHFPathStillResolvesViaLeaf() {
        XCTAssertEqual(ModelIcon.asset(for: "moonshotai/Kimi-Dev-72B"), "model-moonshotai")
    }
}
