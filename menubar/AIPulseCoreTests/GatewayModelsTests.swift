import XCTest
@testable import AIPulseCore

final class GatewayModelsTests: XCTestCase {

    // MARK: - /v1/models parsing + grouping

    private func modelsJSON(_ ids: [String]) -> Data {
        let entries = ids.map { #"{"id": "\#($0)", "object": "model"}"# }.joined(separator: ",")
        return Data(#"{"object": "list", "data": [\#(entries)]}"#.utf8)
    }

    func testParseGroupsRoccoOllamaAnthropic() throws {
        let data = modelsJSON([
            "claude-rocco-wrn", "claude-rocco-kimi",
            "rabbit", "gpt-oss:20b", "whiterabbitneo-70b-tools",
            "opus4.8", "sonnet", "haiku",
        ])
        let models = try GatewayModelList.parse(data)
        XCTAssertEqual(models.filter { $0.group == .rocco }.map(\.id),
                       ["claude-rocco-wrn", "claude-rocco-kimi"])
        XCTAssertEqual(models.filter { $0.group == .ollama }.map(\.id),
                       ["rabbit", "gpt-oss:20b", "whiterabbitneo-70b-tools"])
        XCTAssertEqual(models.filter { $0.group == .anthropic }.map(\.id),
                       ["opus4.8", "sonnet", "haiku"])
    }

    func testParseSkipsWildcards() throws {
        let data = modelsJSON(["rocco-*", "*", "claude-rocco-wrn", "rabbit"])
        let models = try GatewayModelList.parse(data)
        XCTAssertEqual(models.map(\.id), ["claude-rocco-wrn", "rabbit"])
    }

    func testDisplayNamesStripPrefixes() throws {
        let data = modelsJSON(["claude-rocco-wrn", "rabbit", "opus4.8"])
        let models = try GatewayModelList.parse(data)
        XCTAssertEqual(models.map(\.displayName), ["rocco-wrn", "rabbit", "opus4.8"])
    }

    func testParseMalformedThrows() {
        XCTAssertThrowsError(try GatewayModelList.parse(Data("nope".utf8)))
    }

    // MARK: - gateway.env master key

    func testMasterKeyParsing() {
        let env = "GATEWAY_MASTER_KEY=sk-rocco-abc123\nANTHROPIC_REAL_KEY=placeholder\n"
        XCTAssertEqual(GatewayEnv.masterKey(from: env), "sk-rocco-abc123")
        XCTAssertNil(GatewayEnv.masterKey(from: "OTHER=x\n"))
    }

    // MARK: - ~/.claude/settings.json default model

    func testCurrentModelReadsTopLevelKey() {
        let json = Data(#"{"model": "claude-rocco-wrn", "statusLine": {"type": "command"}}"#.utf8)
        XCTAssertEqual(ClaudeDefaultModel.current(settingsJSON: json), "claude-rocco-wrn")
        XCTAssertNil(ClaudeDefaultModel.current(settingsJSON: Data("{}".utf8)))
        XCTAssertNil(ClaudeDefaultModel.current(settingsJSON: nil))
    }

    func testUpdatedPreservesSiblingKeys() throws {
        let json = Data(#"{"statusLine": {"type": "command", "command": "/x.sh"}, "model": "old"}"#.utf8)
        let out = try ClaudeDefaultModel.updated(settingsJSON: json, model: "claude-rocco-kimi")
        let dict = try JSONSerialization.jsonObject(with: out) as! [String: Any]
        XCTAssertEqual(dict["model"] as? String, "claude-rocco-kimi")
        let sl = dict["statusLine"] as! [String: Any]
        XCTAssertEqual(sl["command"] as? String, "/x.sh")
    }

    func testUpdatedFromNilCreatesMinimalSettings() throws {
        let out = try ClaudeDefaultModel.updated(settingsJSON: nil, model: "claude-ollama-rabbit")
        let dict = try JSONSerialization.jsonObject(with: out) as! [String: Any]
        XCTAssertEqual(dict["model"] as? String, "claude-ollama-rabbit")
        XCTAssertEqual(dict.count, 1)
    }
}
