import XCTest
import AppKit
@testable import RoccoPulseCore

final class TierColorTests: XCTestCase {
    func testTierOneIsRed() {
        XCTAssertEqual(TierPalette.color(for: 1), NSColor.systemRed)
    }

    func testTierTwoIsOrange() {
        XCTAssertEqual(TierPalette.color(for: 2), NSColor.systemOrange)
    }

    func testTierThreeIsYellow() {
        XCTAssertEqual(TierPalette.color(for: 3), NSColor.systemYellow)
    }

    func testTierFourIsGreen() {
        XCTAssertEqual(TierPalette.color(for: 4), NSColor.systemGreen)
    }

    func testTierFiveIsMint() {
        XCTAssertEqual(TierPalette.color(for: 5), NSColor.systemMint)
    }

    func testUnknownTierFallsBackToGray() {
        XCTAssertEqual(TierPalette.color(for: 0), NSColor.systemGray)
        XCTAssertEqual(TierPalette.color(for: 6), NSColor.systemGray)
        XCTAssertEqual(TierPalette.color(for: -1), NSColor.systemGray)
        XCTAssertEqual(TierPalette.color(for: nil), NSColor.systemGray)
    }
}
