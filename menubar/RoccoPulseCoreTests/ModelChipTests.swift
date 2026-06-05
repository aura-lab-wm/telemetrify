import XCTest
@testable import RoccoPulseCore

/// Option B ("compact in-card chip") needs model names short enough to
/// sit in a fixed-height capsule without growing the card. The rule:
/// camel-case words with ≥3 capitals compress to their initials
/// ("WhiteRabbitNeo" → "WRN"); everything else stays readable.
final class ModelChipTests: XCTestCase {
    func testHFPathUsesLeafOnly() {
        XCTAssertEqual(ModelChip.label(model: "moonshotai/Kimi-Dev-72B"),
                       "Kimi-Dev-72B")
    }

    func testCamelCaseWordCompressesToInitials() {
        XCTAssertEqual(ModelChip.label(model: "Llama-3.1-WhiteRabbitNeo-2-70B"),
                       "WRN-2-70B")
        XCTAssertEqual(ModelChip.label(model: "WhiteRabbitNeo-70B"),
                       "WRN-70B")
    }

    func testShortWordsStayReadable() {
        // 1-capital words are already compact — leave them alone.
        XCTAssertEqual(ModelChip.label(model: "Qwen3-Coder-30B"),
                       "Qwen3-Coder-30B")
    }

    func testPrecisionSuffixIsUppercased() {
        XCTAssertEqual(ModelChip.label(model: "WhiteRabbitNeo-70B", precision: "bf16"),
                       "WRN-70B · BF16")
        XCTAssertEqual(ModelChip.label(model: "Kimi-Dev-72B", precision: "fp8"),
                       "Kimi-Dev-72B · FP8")
    }

    func testCommonPrefixesAreStripped() {
        XCTAssertEqual(ModelChip.label(model: "Meta-Llama-3.1-70B"),
                       "70B")
    }
}
