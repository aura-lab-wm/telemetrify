import XCTest
@testable import AIPulseCore

// IconState (RoccoStatus.swift) tests — consolidated here from
// SSHProbeArgsTests.swift / StatusStoreTests.swift, where they had been
// stranded in unrelated files (twice the cause of misplaced new tests).

/// Targeted coverage for `IconState.derive` — the menubar icon's whole state
/// machine lives in that one function, so any new case (here:
/// `.agentMissing`) must be pinned by a test.
final class IconStateAgentMissingTests: XCTestCase {
    func testAgentMissingLastErrorYieldsAgentMissingState() {
        let state = IconState.derive(
            snapshot: nil,
            lastError: "rocco-agent not installed on remote (run install.sh)",
            now: Date(),
            errorKind: .agentFileMissing
        )
        XCTAssertEqual(state, .agentMissing)
    }

    func testGenericLastErrorStaysUnreachable() {
        let state = IconState.derive(
            snapshot: nil,
            lastError: "ssh exited with code 255: Permission denied",
            now: Date(),
            errorKind: .sshFailed
        )
        XCTAssertEqual(state, .unreachable)
    }

    /// A non-SSHProbeError (e.g. ProcessLauncher timeout) must NOT be
    /// classified as .unreachable's "ssh down" hint. It bubbles up as
    /// .unknown so the UI shows a generic diagnosis.
    func testUnknownErrorKindStaysUnreachableButDistinguished() {
        let state = IconState.derive(
            snapshot: nil,
            lastError: "process timeout after 6s",
            now: Date(),
            errorKind: .unknown
        )
        // .unknown error kind without a snapshot is still "we couldn't get
        // data" → .unreachable for the icon. The popover differentiates.
        XCTAssertEqual(state, .unreachable)
    }

    func testDecodeFailedErrorKindMapsToAgentMissingIconColor() {
        let state = IconState.derive(
            snapshot: nil,
            lastError: "Could not parse rocco-status.json: bad json",
            now: Date(),
            errorKind: .decodeFailed
        )
        // We deliberately reuse .agentMissing for decode failures so the
        // icon (yellow, dimmed) matches the popover's orange "malformed"
        // header instead of going full red.
        XCTAssertEqual(state, .agentMissing)
    }

    func testFreshSnapshotIgnoresErrorKind() {
        let snap = RoccoStatusFixtures.healthy(now: Date())
        let state = IconState.derive(
            snapshot: snap,
            lastError: nil,
            now: Date(),
            errorKind: nil
        )
        XCTAssertEqual(state, .fresh)
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
