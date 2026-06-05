import XCTest
@testable import AIPulseCore

/// Live status push: a long-lived ssh process runs a REMOTE watcher
/// (backend does all the work) and emits one compact JSON line per
/// status-file change; the client only reassembles lines and decodes.
final class StatusStreamerTests: XCTestCase {
    private func fixtureLine() throws -> Data {
        // Same resolution as ParserTests.loadFixture: bundle first, then
        // walk up from #filePath for setups where the Fixtures folder
        // resource wasn't auto-wired.
        let bundle = Bundle(for: type(of: self))
        let raw: Data
        if let url = bundle.url(forResource: "status-healthy", withExtension: "json") {
            raw = try Data(contentsOf: url)
        } else {
            let candidate = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .appendingPathComponent("Fixtures/status-healthy.json")
            raw = try Data(contentsOf: candidate)
        }
        let obj = try JSONSerialization.jsonObject(with: raw)
        var line = try JSONSerialization.data(withJSONObject: obj)
        line.append(Data("\n".utf8))
        return line
    }

    // MARK: - line reassembly

    func testLineAccumulatorReassemblesArbitraryChunks() {
        var acc = LineAccumulator()
        XCTAssertEqual(acc.ingest(Data("ab".utf8)), [])
        XCTAssertEqual(acc.ingest(Data("c\nde".utf8)), ["abc"])
        XCTAssertEqual(acc.ingest(Data("f\n\ng\n".utf8)), ["def", "g"])
    }

    // MARK: - remote command shape

    func testRemoteWatcherRunsServerSide() {
        let args = StatusStreamer.sshArguments(host: "rocco")
        XCTAssertTrue(args.contains("rocco"))
        XCTAssertTrue(args.contains("BatchMode=yes"))
        let remote = args.last ?? ""
        // the WATCH loop lives on the server: mtime check + fast sleep
        XCTAssertTrue(remote.contains("rocco-status.json"))
        XCTAssertTrue(remote.contains("sleep 0.2"))
        // multi-line JSON is compacted server-side to one frame per line
        XCTAssertTrue(remote.contains("tr -d"))
    }

    // MARK: - ingest: decode, dedupe, resilience

    func testIngestDecodesChunkedFrameOnce() throws {
        let line = try fixtureLine()
        let streamer = StatusStreamer(host: "rocco")
        var got: [RoccoStatus] = []
        streamer.onStatus = { got.append($0) }

        // split the frame into two chunks to prove reassembly
        let mid = line.count / 2
        streamer.ingest(line.prefix(mid))
        streamer.ingest(line.suffix(line.count - mid))
        XCTAssertEqual(got.count, 1)
        XCTAssertFalse(got[0].gpus.isEmpty)
    }

    func testIdenticalFramesAreDeduped() throws {
        let line = try fixtureLine()
        let streamer = StatusStreamer(host: "rocco")
        var count = 0
        streamer.onStatus = { _ in count += 1 }
        streamer.ingest(line)
        streamer.ingest(line)
        XCTAssertEqual(count, 1)
    }

    func testMalformedFramesAreIgnored() {
        let streamer = StatusStreamer(host: "rocco")
        var count = 0
        streamer.onStatus = { _ in count += 1 }
        streamer.ingest(Data("ssh banner noise, not json\n".utf8))
        XCTAssertEqual(count, 0)
    }
}
