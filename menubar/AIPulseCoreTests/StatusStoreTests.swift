import XCTest
@testable import AIPulseCore

/// In-process fake — lets us pin StatusStore.apply's error classification
/// without forking ssh. The probe throws whatever we say it throws.
final class StubProbe: SSHProbe, @unchecked Sendable {
    var nextResult: Result<RoccoStatus, Error> = .failure(
        SSHProbeError.nonZeroExit(255, "init"))

    override func fetchStatus() throws -> RoccoStatus {
        try nextResult.get()
    }
}

@MainActor
final class StatusStoreTests: XCTestCase {

    private func makeStore() -> (StatusStore, StubProbe) {
        let probe = StubProbe()
        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("ai-pulse-tests-\(UUID().uuidString).json")
        let store = StatusStore(probe: probe, persistenceURL: tempURL)
        return (store, probe)
    }

    func testSuccessClearsBothLastErrorAndLastErrorKind() async {
        let (store, probe) = makeStore()
        // seed an error first
        probe.nextResult = .failure(SSHProbeError.agentFileMissing(
            "cat: /home/x/.cache/rocco-status.json: No such file or directory"))
        await store.refresh()
        XCTAssertNotNil(store.lastError)
        XCTAssertEqual(store.lastErrorKind, .agentFileMissing)

        // then succeed
        probe.nextResult = .success(RoccoStatusFixtures.healthy(now: Date()))
        await store.refresh()
        XCTAssertNil(store.lastError, "lastError must be cleared on success")
        XCTAssertNil(store.lastErrorKind,
                     "lastErrorKind must be cleared on success")
    }

    func testSSHProbeErrorKindIsPropagated() async {
        let (store, probe) = makeStore()
        probe.nextResult = .failure(SSHProbeError.decodeFailed(
            NSError(domain: "test", code: 0)))
        await store.refresh()
        XCTAssertEqual(store.lastErrorKind, .decodeFailed)
    }

    func testGenericErrorBucketedAsUnknownNotSshFailed() async {
        let (store, probe) = makeStore()
        // any error type that is NOT SSHProbeError
        probe.nextResult = .failure(NSError(
            domain: "ProcessLauncher", code: 408,
            userInfo: [NSLocalizedDescriptionKey: "process timeout after 6s"]))
        await store.refresh()
        XCTAssertEqual(store.lastErrorKind, .unknown,
            "non-SSHProbeError must NOT be misclassified as .sshFailed — that hint sends the user to ssh-add for problems that aren't SSH")
        XCTAssertNotNil(store.lastError)
    }

    func testLegacyCacheMigratesOnceAndNeverOverwrites() throws {
        let fm = FileManager.default
        let dir = fm.temporaryDirectory
            .appendingPathComponent("ai-pulse-migrate-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: dir) }
        let legacy = dir.appendingPathComponent("legacy.json")
        let current = dir.appendingPathComponent("current.json")
        try Data("old".utf8).write(to: legacy)

        StatusStore.migrateLegacyCache(legacy: legacy, current: current)
        XCTAssertTrue(fm.fileExists(atPath: current.path))
        XCTAssertFalse(fm.fileExists(atPath: legacy.path))

        try Data("newer".utf8).write(to: legacy)
        StatusStore.migrateLegacyCache(legacy: legacy, current: current)
        XCTAssertEqual(try String(contentsOf: current, encoding: .utf8), "old",
                       "an existing cache must never be overwritten")
    }

    func testIsLiveTracksRecentStreamFrames() {
        let (store, _) = makeStore()
        XCTAssertFalse(store.isLive(), "no frames yet -> watchdog mode")

        store.lastStreamFrameAt = Date()
        XCTAssertTrue(store.isLive())

        store.lastStreamFrameAt = Date(timeIntervalSinceNow: -30)
        XCTAssertFalse(store.isLive(), "stale frames mean the stream is down")
    }
}
