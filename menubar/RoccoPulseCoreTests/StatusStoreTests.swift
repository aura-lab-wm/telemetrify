import XCTest
@testable import RoccoPulseCore

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
            .appendingPathComponent("rocco-pulse-tests-\(UUID().uuidString).json")
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
}

/// Locks the legacy three-arg `IconState.derive(snapshot:lastError:now:)`
/// overload's behavior so a refactor can't silently break callers that
/// haven't been migrated to the four-arg form (preview, widget, embedder).
final class IconStateLegacyOverloadTests: XCTestCase {
    func testLegacyOverloadFallsBackToUnreachableForAnyError() {
        let state = IconState.derive(
            snapshot: nil,
            lastError: "ssh exited with code 255: Permission denied",
            now: Date()
        )
        XCTAssertEqual(state, .unreachable)
    }

    func testLegacyOverloadReturnsUnknownWhenNoSnapshotAndNoError() {
        let state = IconState.derive(snapshot: nil, lastError: nil, now: Date())
        XCTAssertEqual(state, .unknown)
    }

    func testLegacyOverloadPrefersFreshSnapshot() {
        let snap = RoccoStatusFixtures.healthy(now: Date())
        let state = IconState.derive(snapshot: snap, lastError: nil, now: Date())
        XCTAssertEqual(state, .fresh)
    }
}
