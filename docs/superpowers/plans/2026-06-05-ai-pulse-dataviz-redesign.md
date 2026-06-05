# AI-Pulse Data-Viz Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Design #4 "Data-Viz Forward" spec (`design-explorations/ai-pulse-design4-final.html`) in the SwiftUI menubar app: service card + fixed 44pt action gutter (the layout-stability fix), 2×2 radial GPU gauges with memory sparklines + KPI strip, and an upgraded log inspector (level chips, search, auto-scroll, copy).

**Architecture:** Pure presentation logic (gutter mapping, GPU history ring buffer, KPI aggregates, log-line parsing/filtering) goes into the `RoccoPulseCore` framework where the existing XCTest suite lives — TDD each. SwiftUI views in `RoccoPulseApp` consume those types; views are verified by building (`make build`) since the app target has no test bundle. The non-negotiable layout rule: every service row is `HStack { card; gutter.frame(width: 44) }` — the lifecycle action is an icon-only button OUTSIDE the card; the log-inspect (spotlight) button is INSIDE the card, always at the same trailing position, in every state.

**Tech Stack:** Swift 5 / SwiftUI, macOS 14 deployment target, xcodegen + xcodebuild via `menubar/Makefile`. No new dependencies (gauges/sparklines are hand-drawn `Circle.trim` / `Path` — no Swift Charts import needed).

**Working directory for all commands:** `/Users/amastro/Projects/telemetrify/menubar`

**Test command (whole core suite — there is no single-test make target):**
`make test` (runs `xcodebuild -project rocco-pulse.xcodeproj -scheme RoccoPulseCore test`)
To run one test class faster:
`xcodebuild -project rocco-pulse.xcodeproj -scheme RoccoPulseCore test CODE_SIGNING_ALLOWED=NO -derivedDataPath build -only-testing:RoccoPulseCoreTests/<ClassName>`

**Reference spec:** `/Users/amastro/Projects/telemetrify/design-explorations/ai-pulse-design4-final.html`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `RoccoPulseCore/Sources/GutterPresentation.swift` | Create | Pure mapping (action, inFlight) → gutter icon/busy/placeholder |
| `RoccoPulseCoreTests/GutterPresentationTests.swift` | Create | TDD for the mapping |
| `RoccoPulseCore/Sources/GPUHistory.swift` | Create | Per-GPU memory ring buffer for sparklines + KPI aggregates (`GPUSummary`) |
| `RoccoPulseCoreTests/GPUHistoryTests.swift` | Create | TDD for ring buffer + aggregates |
| `RoccoPulseCore/Sources/StatusStore.swift` | Modify | Append each successful snapshot to published `gpuHistory` |
| `RoccoPulseCore/Sources/LogLine.swift` | Create | Log line level parsing + `LogFilter` (levels + query) |
| `RoccoPulseCoreTests/LogLineTests.swift` | Create | TDD for parsing/filtering |
| `RoccoPulseApp/Sources/ServicesSection.swift` | Modify | `ServiceRowView` → card + 44pt gutter layout |
| `RoccoPulseApp/Sources/GPUGridSection.swift` | Create | Radial gauge, sparkline, GPU cell, 2×2 grid, KPI strip |
| `RoccoPulseApp/Sources/StatusView.swift` | Modify | Swap `gpuRow`/`UnifiedUsageBar` for `GPUGridSection` |
| `RoccoPulseApp/Sources/LogInspectorView.swift` | Modify | Colored line stream, level chips, search, auto-scroll, copy |

---

### Task 1: Core — `GutterPresentation` mapping (TDD)

The gutter must show exactly one of: an action icon (play/stop/restart/open), a busy spinner, or a ghost placeholder that reserves the width. This is a pure function of `(ServiceAction?, inFlight)` and is the type the view switches on.

**Files:**
- Create: `RoccoPulseCore/Sources/GutterPresentation.swift`
- Test: `RoccoPulseCoreTests/GutterPresentationTests.swift`

- [ ] **Step 1: Write the failing test**

```swift
import XCTest
@testable import RoccoPulseCore

final class GutterPresentationTests: XCTestCase {
    private func action(_ command: ServiceCommand) -> ServiceAction {
        ServiceAction(label: "x", showWhen: [.up, .down, .unknown], command: command)
    }

    func testInFlightWinsOverAnyAction() {
        let p = GutterPresentation.make(action: action(.startVLLM), inFlight: true)
        XCTAssertEqual(p, .busy)
    }

    func testNilActionIsPlaceholder() {
        XCTAssertEqual(GutterPresentation.make(action: nil, inFlight: false), .placeholder)
    }

    func testStopCommandsAreDestructiveStopIcon() {
        for cmd: ServiceCommand in [.stopVLLM, .stopLocalAgent(label: "l")] {
            let p = GutterPresentation.make(action: action(cmd), inFlight: false)
            XCTAssertEqual(p, .action(symbol: "stop.fill", verb: "Stop", isDestructive: true))
        }
    }

    func testStartCommandsArePlayIcon() {
        for cmd: ServiceCommand in [.startVLLM, .startLocalAgent(label: "l")] {
            let p = GutterPresentation.make(action: action(cmd), inFlight: false)
            XCTAssertEqual(p, .action(symbol: "play.fill", verb: "Start", isDestructive: false))
        }
    }

    func testRestartCommandsAreClockwiseIcon() {
        let cmds: [ServiceCommand] = [
            .sshRestartUnit(host: "rocco", unit: "rocco-agent.service"),
            .restartLocalAgent(label: "l"),
        ]
        for cmd in cmds {
            let p = GutterPresentation.make(action: action(cmd), inFlight: false)
            XCTAssertEqual(p, .action(symbol: "arrow.clockwise", verb: "Restart", isDestructive: false))
        }
    }

    func testOpenURLIsOutwardArrow() {
        let p = GutterPresentation.make(
            action: action(.openURL(URL(string: "http://x")!)), inFlight: false)
        XCTAssertEqual(p, .action(symbol: "arrow.up.right.square", verb: "Open", isDestructive: false))
    }

    func testSelectModelNeverRendersAGutterButton() {
        // model selection lives in the in-card picker; the gutter must
        // reserve space but show nothing actionable.
        let p = GutterPresentation.make(action: action(.selectModel(profile: 2)), inFlight: false)
        XCTAssertEqual(p, .placeholder)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: FAIL — `cannot find 'GutterPresentation' in scope` (compile error counts as the failing state).

- [ ] **Step 3: Write minimal implementation**

Create `RoccoPulseCore/Sources/GutterPresentation.swift`:

```swift
import Foundation

/// What the fixed-width action gutter OUTSIDE a service card renders.
/// The design rule (docs: design-explorations/ai-pulse-design4-final.html)
/// is that every service row is `HStack { card; gutter(width: 44) }` —
/// the lifecycle action never lives inside the card, so the card's
/// internal layout is identical in every state. This enum is the single
/// source of truth for what the gutter shows; the view just switches.
public enum GutterPresentation: Equatable, Sendable {
    /// Icon-only lifecycle button (play/stop/restart/open).
    case action(symbol: String, verb: String, isDestructive: Bool)
    /// An action is in flight — spinner, no button.
    case busy
    /// No action for this state — dashed ghost so the width stays reserved.
    case placeholder

    public static func make(action: ServiceAction?, inFlight: Bool) -> GutterPresentation {
        if inFlight { return .busy }
        guard let action else { return .placeholder }
        switch action.command {
        case .stopVLLM, .stopLocalAgent:
            return .action(symbol: "stop.fill", verb: "Stop", isDestructive: true)
        case .startVLLM, .startLocalAgent:
            return .action(symbol: "play.fill", verb: "Start", isDestructive: false)
        case .sshRestartUnit, .restartLocalAgent:
            return .action(symbol: "arrow.clockwise", verb: "Restart", isDestructive: false)
        case .openURL:
            return .action(symbol: "arrow.up.right.square", verb: "Open", isDestructive: false)
        case .selectModel:
            // Model selection is the in-card dropdown, never a gutter button.
            return .placeholder
        }
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: PASS — all `GutterPresentationTests` green, no other suite regressions.

- [ ] **Step 5: Commit**

```bash
cd /Users/amastro/Projects/telemetrify
git add menubar/RoccoPulseCore/Sources/GutterPresentation.swift menubar/RoccoPulseCoreTests/GutterPresentationTests.swift
git commit -m "feat(pulse): add GutterPresentation mapping for fixed action gutter"
```

---

### Task 2: App — `ServiceRowView` card + 44pt gutter

Restructure each service row into a visually distinct card with the spotlight (log) button pinned at the card's trailing edge in EVERY state, and the lifecycle action as an icon-only button in a fixed 44pt gutter OUTSIDE the card. This fixes the misalignment bug where the Start button pushed the spotlight icon to a different x-position.

**Files:**
- Modify: `RoccoPulseApp/Sources/ServicesSection.swift` (the `ServiceRowView` struct, ~lines 285–533)

- [ ] **Step 1: Update `Metrics` and `body`**

In `ServiceRowView`, replace the `Metrics` enum and the `body`/`primaryRow` properties with:

```swift
    private enum Metrics {
        static let dot: CGFloat = 10
        static let icon: CGFloat = 22
        static let name: CGFloat = 108
        static let spotlight: CGFloat = 34   // in-card, ALWAYS reserved
        static let gutter: CGFloat = 44      // outside-card action column
        static let rowHeight: CGFloat = 32
        static let columnGap: CGFloat = 8
        static let detailIndent: CGFloat = dot + columnGap + icon + columnGap
    }

    var body: some View {
        // Card + gutter are SIBLINGS. The lifecycle action never enters
        // the card, so the card's frame is identical whether the service
        // is up, down, or transitioning — that's the whole point.
        HStack(alignment: .center, spacing: 8) {
            card
            actionGutter
                .frame(width: Metrics.gutter)
        }
        .padding(.vertical, 2)
        .help(row.status.error ?? row.service.displayName)
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 4) {
            primaryRow
            if hasSecondaryDetail {
                secondaryDetailRow
            }
            if !logLines.isEmpty {
                ServiceLogStrip(lines: logLines, isLive: inFlight)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(0.05))
        )
    }

    private var primaryRow: some View {
        HStack(alignment: .center, spacing: Metrics.columnGap) {
            statusDot
            serviceIcon
            serviceName
                .frame(width: Metrics.name, alignment: .leading)
            summaryText
                .frame(maxWidth: .infinity, alignment: .leading)
            spotlightButton   // pinned trailing IN-CARD, same x every row
        }
        .frame(minHeight: Metrics.rowHeight)
    }

    /// The log-inspect button. When a service has no log files we still
    /// reserve the exact same width so the trailing edge never drifts.
    @ViewBuilder
    private var spotlightButton: some View {
        if row.service.logFiles.isEmpty {
            Color.clear
                .frame(width: Metrics.spotlight, height: 22)
        } else {
            Button { onInspectLogs() } label: {
                Image(systemName: "text.magnifyingglass")
                    .font(.system(size: 13, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .frame(width: Metrics.spotlight - 6, height: 22)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .frame(width: Metrics.spotlight)
            .help("Inspect logs")
            .accessibilityLabel("Inspect \(row.service.displayName) logs")
        }
    }

    /// Fixed-width lifecycle column OUTSIDE the card. Icon-only.
    /// Switches on the Core-tested GutterPresentation so this view has
    /// zero mapping logic of its own.
    @ViewBuilder
    private var actionGutter: some View {
        let currentAction = row.service.action(for: row.status.state)
        switch GutterPresentation.make(action: currentAction, inFlight: inFlight) {
        case .busy:
            ProgressView()
                .controlSize(.small)
                .frame(width: 32, height: 32)
        case .placeholder:
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.10),
                              style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                .frame(width: 32, height: 32)
                .accessibilityHidden(true)
        case .action(let symbol, let verb, let isDestructive):
            Button {
                if let currentAction { onAction(currentAction) }
            } label: {
                Image(systemName: symbol)
                    .font(.system(size: 13, weight: .bold))
                    .frame(width: 26, height: 26)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(isDestructive ? .red : .accentColor)
            .help("\(verb) \(row.service.displayName)")
            .accessibilityLabel("\(verb) \(row.service.displayName)")
        }
    }
```

- [ ] **Step 2: Delete the now-dead pieces**

In the same file remove:
- the old `controlCluster` computed property entirely (the spotlight + labeled button cluster);
- the `buttonTint(for:)` helper at the bottom of `ServiceRowView` (tint now comes from `GutterPresentation.isDestructive`);
- the old `Metrics.actions` constant (already gone via Step 1's `Metrics` replacement).

Keep `statusDot`, `serviceIcon`, `serviceName`, `summaryText`, `secondaryDetailRow`, `hasSecondaryDetail`, `modelPickerView`, `trainingHintView`, `modelMenu`, `currentModelShortLabel`, `shortModelName`, `stateColor` — unchanged. `ServiceLogStrip` stays as-is (it now nests inside the card; its own leading padding still reads fine).

- [ ] **Step 3: Build**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make build`
Expected: `BUILD SUCCEEDED`. If `text.magnifyingglass` is unavailable on the macOS 14 SDK, fall back to `doc.text.magnifyingglass` (the previous symbol).

- [ ] **Step 4: Run the core suite (regression)**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: PASS (no core code changed in this task; confirms project still generates).

- [ ] **Step 5: Commit**

```bash
cd /Users/amastro/Projects/telemetrify
git add menubar/RoccoPulseApp/Sources/ServicesSection.swift
git commit -m "feat(pulse): service rows become card + fixed 44pt action gutter"
```

---

### Task 3: Core — `GPUHistory` ring buffer + `GPUSummary` aggregates (TDD)

Sparklines need a short per-GPU memory history accumulated across polls; the KPI strip needs avg util / avg mem / max temp. Both are pure types in Core.

**Files:**
- Create: `RoccoPulseCore/Sources/GPUHistory.swift`
- Test: `RoccoPulseCoreTests/GPUHistoryTests.swift`
- Modify: `RoccoPulseCore/Sources/StatusStore.swift:24-27` (add published history) and `:65-82` (append on success)

- [ ] **Step 1: Write the failing tests**

`GPUHistory.append(gpus:)` deliberately takes `[RoccoStatus.GPU]`, not a full snapshot, so tests only need the GPU initializer. Check `RoccoPulseCoreTests/ParserTests.swift` for how existing tests construct `RoccoStatus.GPU`; if there is no public memberwise init visible even under `@testable import`, decode a tiny JSON fragment using the existing CodingKeys (`idx`, `name`, `util_pct`, `mem_used_mib`, `mem_total_mib`, `temp_c`) via a small `gpu(...)` helper in the test file.

```swift
import XCTest
@testable import RoccoPulseCore

final class GPUHistoryTests: XCTestCase {
    private func gpu(idx: Int, util: Int = 50, usedMib: Int = 500,
                     totalMib: Int = 1000, temp: Int = 60) -> RoccoStatus.GPU {
        // Adapt to the established fixture pattern in this test bundle.
        RoccoStatus.GPU(idx: idx, name: "NVIDIA L40S", utilPct: util,
                        memUsedMib: usedMib, memTotalMib: totalMib, tempC: temp)
    }

    func testAppendRecordsMemFractionPerGPU() {
        var h = GPUHistory(capacity: 4)
        h.append(gpus: [gpu(idx: 0, usedMib: 250, totalMib: 1000),
                        gpu(idx: 1, usedMib: 750, totalMib: 1000)])
        XCTAssertEqual(h.samples(for: 0), [0.25])
        XCTAssertEqual(h.samples(for: 1), [0.75])
    }

    func testCapacityEvictsOldestFirst() {
        var h = GPUHistory(capacity: 3)
        for used in [100, 200, 300, 400] {
            h.append(gpus: [gpu(idx: 0, usedMib: used, totalMib: 1000)])
        }
        XCTAssertEqual(h.samples(for: 0), [0.2, 0.3, 0.4])
    }

    func testUnknownGPUReturnsEmpty() {
        XCTAssertEqual(GPUHistory(capacity: 3).samples(for: 9), [])
    }

    func testSummaryAveragesAndMax() {
        let s = GPUSummary(gpus: [
            gpu(idx: 0, util: 98, usedMib: 900, totalMib: 1000, temp: 69),
            gpu(idx: 1, util: 98, usedMib: 880, totalMib: 1000, temp: 70),
            gpu(idx: 2, util: 96, usedMib: 880, totalMib: 1000, temp: 64),
            gpu(idx: 3, util: 96, usedMib: 880, totalMib: 1000, temp: 57),
        ])
        XCTAssertEqual(s?.avgUtilPct, 97)
        XCTAssertEqual(s?.avgMemPct, 89)   // (90+88+88+88)/4 = 88.5 → rounds to 89
        XCTAssertEqual(s?.maxTempC, 70)
    }

    func testSummaryNilForEmpty() {
        XCTAssertNil(GPUSummary(gpus: []))
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: FAIL — `cannot find 'GPUHistory' in scope`.

- [ ] **Step 3: Write minimal implementation**

Create `RoccoPulseCore/Sources/GPUHistory.swift`:

```swift
import Foundation

/// Rolling per-GPU memory history feeding the popover sparklines.
/// Bounded ring buffer: one Double in [0,1] per poll per GPU. ~40
/// samples ≈ 10 minutes at the default 15s poll — enough to read a
/// trend, cheap enough to redraw every tick.
public struct GPUHistory: Equatable, Sendable {
    public let capacity: Int
    private var samplesByGPU: [Int: [Double]] = [:]

    public init(capacity: Int = 40) {
        self.capacity = max(1, capacity)
    }

    public mutating func append(gpus: [RoccoStatus.GPU]) {
        for gpu in gpus {
            var s = samplesByGPU[gpu.idx] ?? []
            s.append(min(1, max(0, gpu.memPctUsed / 100.0)))
            if s.count > capacity { s.removeFirst(s.count - capacity) }
            samplesByGPU[gpu.idx] = s
        }
    }

    public func samples(for idx: Int) -> [Double] {
        samplesByGPU[idx] ?? []
    }
}

/// Fleet-level aggregates for the KPI strip above the gauge grid.
public struct GPUSummary: Equatable, Sendable {
    public let avgUtilPct: Int
    public let avgMemPct: Int
    public let maxTempC: Int

    public init?(gpus: [RoccoStatus.GPU]) {
        guard !gpus.isEmpty else { return nil }
        let n = Double(gpus.count)
        avgUtilPct = Int((gpus.map { Double($0.utilPct) }.reduce(0, +) / n).rounded())
        avgMemPct  = Int((gpus.map { $0.memPctUsed }.reduce(0, +) / n).rounded())
        maxTempC   = gpus.map { $0.tempC }.max() ?? 0
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: PASS — `GPUHistoryTests` green.

- [ ] **Step 5: Wire history into `StatusStore`**

In `RoccoPulseCore/Sources/StatusStore.swift`, add below the other `@Published` properties (line ~27):

```swift
    @Published public private(set) var gpuHistory = GPUHistory()
```

and in `apply(result:)`, inside `case .success(let status):` directly after `self.snapshot = status`:

```swift
            self.gpuHistory.append(gpus: status.gpus)
```

- [ ] **Step 6: Run the full suite (StatusStoreTests must stay green)**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd /Users/amastro/Projects/telemetrify
git add menubar/RoccoPulseCore/Sources/GPUHistory.swift menubar/RoccoPulseCoreTests/GPUHistoryTests.swift menubar/RoccoPulseCore/Sources/StatusStore.swift
git commit -m "feat(pulse): GPU memory history ring buffer + KPI aggregates"
```

---

### Task 4: App — radial gauges, sparklines, KPI strip (`GPUGridSection`)

Replace the stacked util/mem bars with the Data-Viz Forward layout: a KPI strip (avg util / avg mem / max temp) and a 2×2 grid of GPU cells, each cell = ring gauge (util) + memory sparkline + monospace metrics.

**Files:**
- Create: `RoccoPulseApp/Sources/GPUGridSection.swift`
- Modify: `RoccoPulseApp/Sources/StatusView.swift:116-127` (call site) and `:204-274` (delete `gpuRow` + `UnifiedUsageBar`)

- [ ] **Step 1: Create `GPUGridSection.swift`**

```swift
import SwiftUI
import RoccoPulseCore

/// Data-viz GPU block: KPI strip + 2×2 grid of ring-gauge cells.
/// Spec: design-explorations/ai-pulse-design4-final.html ("Data-Viz
/// Forward"). Rings are hand-drawn Circle.trim (no Charts dependency);
/// the sparkline is a Path over GPUHistory samples.
struct GPUGridSection: View {
    let gpus: [RoccoStatus.GPU]
    let history: GPUHistory

    private let columns = [
        GridItem(.flexible(), spacing: 8),
        GridItem(.flexible(), spacing: 8),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let summary = GPUSummary(gpus: gpus) {
                KPIStrip(summary: summary)
            }
            LazyVGrid(columns: columns, spacing: 8) {
                ForEach(gpus, id: \.idx) { gpu in
                    GPUCell(gpu: gpu, memSamples: history.samples(for: gpu.idx))
                }
            }
        }
    }
}

private struct KPIStrip: View {
    let summary: GPUSummary

    var body: some View {
        HStack(spacing: 8) {
            kpi(label: "AVG UTIL", value: "\(summary.avgUtilPct)%", color: .blue)
            kpi(label: "AVG MEM", value: "\(summary.avgMemPct)%", color: .purple)
            kpi(label: "MAX TEMP", value: "\(summary.maxTempC)°C",
                color: tempColor(summary.maxTempC))
        }
    }

    private func kpi(label: String, value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.tertiary)
                .tracking(0.5)
            Text(value)
                .font(.system(.callout, design: .monospaced).bold())
                .foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(color.opacity(0.09))
        )
    }
}

private struct GPUCell: View {
    let gpu: RoccoStatus.GPU
    let memSamples: [Double]

    var body: some View {
        HStack(spacing: 8) {
            RingGauge(fraction: Double(gpu.utilPct) / 100.0)
                .frame(width: 40, height: 40)
                .overlay(
                    Text("\(gpu.utilPct)")
                        .font(.system(size: 11, design: .monospaced).bold())
                )
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text("GPU \(gpu.idx)")
                        .font(.caption.bold())
                    Spacer(minLength: 0)
                    Text("\(gpu.tempC)°")
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundStyle(tempColor(gpu.tempC))
                }
                Sparkline(samples: memSamples)
                    .frame(height: 12)
                Text("mem \(Int(gpu.memPctUsed))%")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(7)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(0.05))
        )
        .help("\(gpu.name) — \(gpu.utilPct)% util · \(Int(gpu.memPctUsed))% mem · \(gpu.tempC)°C")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU \(gpu.idx)")
        .accessibilityValue("util \(gpu.utilPct) percent, memory \(Int(gpu.memPctUsed)) percent, \(gpu.tempC) degrees")
    }
}

/// Utilization ring: gray track + trimmed arc, blue→orange→red as load climbs.
private struct RingGauge: View {
    let fraction: Double

    var body: some View {
        let clamped = min(1, max(0, fraction))
        ZStack {
            Circle()
                .stroke(Color.primary.opacity(0.10), lineWidth: 4)
            Circle()
                .trim(from: 0, to: clamped)
                .stroke(ringColor(clamped),
                        style: StrokeStyle(lineWidth: 4, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
    }

    private func ringColor(_ f: Double) -> Color {
        if f >= 0.95 { return .red }
        if f >= 0.80 { return .orange }
        return .blue
    }
}

/// Memory history sparkline. Empty/one-sample history draws a flat
/// baseline so cells never collapse before the second poll lands.
private struct Sparkline: View {
    let samples: [Double]

    var body: some View {
        GeometryReader { geo in
            let pts = points(in: geo.size)
            Path { p in
                guard let first = pts.first else { return }
                p.move(to: first)
                for pt in pts.dropFirst() { p.addLine(to: pt) }
            }
            .stroke(Color.purple, style: StrokeStyle(lineWidth: 1.5,
                                                     lineCap: .round,
                                                     lineJoin: .round))
        }
    }

    private func points(in size: CGSize) -> [CGPoint] {
        let values = samples.isEmpty ? [0, 0] :
            (samples.count == 1 ? [samples[0], samples[0]] : samples)
        let stepX = size.width / CGFloat(values.count - 1)
        return values.enumerated().map { i, v in
            CGPoint(x: CGFloat(i) * stepX,
                    y: size.height - CGFloat(min(1, max(0, v))) * size.height)
        }
    }
}

private func tempColor(_ c: Int) -> Color {
    if c >= 80 { return .red }
    if c >= 65 { return .orange }
    return .green
}
```

- [ ] **Step 2: Swap the call site in `StatusView.swift`**

In `roccoPane`, replace:

```swift
                if snapshot.gpus.isEmpty {
                    Text("No GPUs visible")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    VStack(spacing: 6) {
                        ForEach(snapshot.gpus, id: \.idx) { gpu in
                            gpuRow(gpu)
                        }
                    }
                }
```

with:

```swift
                if snapshot.gpus.isEmpty {
                    Text("No GPUs visible")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    GPUGridSection(gpus: snapshot.gpus,
                                   history: store.gpuHistory)
                }
```

Then delete the entire `// MARK: - GPU row` section: `gpuRow(_:)` and the `UnifiedUsageBar` struct (StatusView.swift lines ~204–274). Nothing else references them.

- [ ] **Step 3: Build**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make build`
Expected: `BUILD SUCCEEDED`.

- [ ] **Step 4: Commit**

```bash
cd /Users/amastro/Projects/telemetrify
git add menubar/RoccoPulseApp/Sources/GPUGridSection.swift menubar/RoccoPulseApp/Sources/StatusView.swift
git commit -m "feat(pulse): 2x2 radial GPU gauges with mem sparklines + KPI strip"
```

---

### Task 5: Core — log line parsing + filtering (TDD)

The upgraded log inspector renders per-line with level colors and supports level-chip + search filtering. Parsing/filtering is pure → Core.

**Files:**
- Create: `RoccoPulseCore/Sources/LogLine.swift`
- Test: `RoccoPulseCoreTests/LogLineTests.swift`

- [ ] **Step 1: Write the failing tests**

```swift
import XCTest
@testable import RoccoPulseCore

final class LogLineTests: XCTestCase {
    func testParseAssignsStableIDsInOrder() {
        let lines = LogLine.parse("a\nb\nc")
        XCTAssertEqual(lines.map(\.id), [0, 1, 2])
        XCTAssertEqual(lines.map(\.text), ["a", "b", "c"])
    }

    func testParseSkipsEmptyLines() {
        XCTAssertEqual(LogLine.parse("a\n\n\nb").map(\.text), ["a", "b"])
    }

    func testLevelDetection() {
        XCTAssertEqual(LogLine.parse("ERROR: CUDA out of memory").first?.level, .error)
        XCTAssertEqual(LogLine.parse("[2026-06-05] WARNING kv-cache nearly full").first?.level, .warn)
        XCTAssertEqual(LogLine.parse("INFO:     Started server process").first?.level, .info)
        XCTAssertEqual(LogLine.parse("Traceback (most recent call last):").first?.level, .error)
        XCTAssertEqual(LogLine.parse("model load failed after 3 retries").first?.level, .error)
        // Plain lines have no level — they're untagged, not info.
        XCTAssertNil(LogLine.parse("just some output").first?.level)
    }

    func testFilterByLevelKeepsUntaggedLines() {
        let lines = LogLine.parse("INFO ok\nERROR boom\nplain")
        var f = LogFilter()
        f.enabledLevels = [.error]
        // untagged lines always pass the level filter; only tagged
        // lines of a DISABLED level are hidden.
        XCTAssertEqual(f.apply(to: lines).map(\.text), ["ERROR boom", "plain"])
    }

    func testFilterByQueryIsCaseInsensitive() {
        let lines = LogLine.parse("loading WhiteRabbitNeo\nGET /health 200")
        var f = LogFilter()
        f.query = "whiterabbit"
        XCTAssertEqual(f.apply(to: lines).map(\.text), ["loading WhiteRabbitNeo"])
    }

    func testDefaultFilterPassesEverything() {
        let lines = LogLine.parse("INFO a\nWARN b\nERROR c\nplain")
        XCTAssertEqual(LogFilter().apply(to: lines).count, 4)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: FAIL — `cannot find 'LogLine' in scope`.

- [ ] **Step 3: Write minimal implementation**

Create `RoccoPulseCore/Sources/LogLine.swift`:

```swift
import Foundation

public enum LogLevel: String, CaseIterable, Equatable, Sendable {
    case info, warn, error
}

/// One rendered log line. `id` is the line's position in the loaded
/// tail — stable for the lifetime of one load, which is all SwiftUI's
/// ForEach/ScrollViewReader need.
public struct LogLine: Equatable, Identifiable, Sendable {
    public let id: Int
    public let text: String
    public let level: LogLevel?

    public static func parse(_ raw: String) -> [LogLine] {
        raw.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
            .map(String.init)
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            .enumerated()
            .map { i, text in LogLine(id: i, text: text, level: detectLevel(text)) }
    }

    private static func detectLevel(_ text: String) -> LogLevel? {
        let l = text.lowercased()
        if l.contains("error") || l.contains("traceback")
            || l.contains("exception") || l.contains("critical")
            || l.contains("failed") || l.contains("fatal") {
            return .error
        }
        if l.contains("warn") || l.contains("timeout") || l.contains("retry") {
            return .warn
        }
        if l.contains("info") || l.contains("debug") {
            return .info
        }
        return nil
    }
}

/// Level chips + search box state, applied as a pure function so the
/// view never re-implements filtering. Untagged lines always pass the
/// level filter — hiding them would make a "show errors only" view
/// drop the stack-trace body lines that follow an ERROR header.
public struct LogFilter: Equatable, Sendable {
    public var enabledLevels: Set<LogLevel> = Set(LogLevel.allCases)
    public var query: String = ""

    public init() {}

    public func apply(to lines: [LogLine]) -> [LogLine] {
        lines.filter { line in
            if let level = line.level, !enabledLevels.contains(level) {
                return false
            }
            guard !query.isEmpty else { return true }
            return line.text.localizedCaseInsensitiveContains(query)
        }
    }
}
```

NOTE: "model load failed after 3 retries" contains both `failed` and `retr` — error wins because the error branch is checked first. That matches the test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: PASS — `LogLineTests` green.

- [ ] **Step 5: Commit**

```bash
cd /Users/amastro/Projects/telemetrify
git add menubar/RoccoPulseCore/Sources/LogLine.swift menubar/RoccoPulseCoreTests/LogLineTests.swift
git commit -m "feat(pulse): log line level parsing + filter model"
```

---

### Task 6: App — log inspector upgrade

Per-line colored stream, level filter chips, search field, auto-scroll toggle, copy-filtered-text button — per the spec's log-viewer section. The insight pane and window plumbing stay as-is.

**Files:**
- Modify: `RoccoPulseApp/Sources/LogInspectorView.swift` (the `LogInspectorView` struct + `LogInspectorModel`)

- [ ] **Step 1: Extend `LogInspectorModel` with parsed lines**

In `LogInspectorModel` (LogInspectorView.swift:201-256), add a published property after `@Published var insight`:

```swift
    @Published var lines: [LogLine] = []
```

and in `load(_:)`, after `text = loaded`:

```swift
            lines = LogLine.parse(loaded)
```

and in the `catch` branch, after `text = error.localizedDescription`:

```swift
            lines = LogLine.parse(error.localizedDescription)
```

- [ ] **Step 2: Replace `rawLogPane` with the filterable stream**

In `LogInspectorView`, add state below `@State private var selectedID`:

```swift
    @State private var filter = LogFilter()
    @State private var autoScroll = true
```

Replace the `rawLogPane` computed property with:

```swift
    private var rawLogPane: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("LOG")
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .tracking(1)
                Spacer()
                Button {
                    copyFiltered()
                } label: {
                    Label("Copy", systemImage: "doc.on.doc")
                }
                .controlSize(.small)
                .help("Copy the filtered lines to the clipboard")
            }
            filterBar
            if model.isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                logStream
            }
        }
        .padding(14)
    }

    private var filterBar: some View {
        HStack(spacing: 8) {
            HStack(spacing: 4) {
                Image(systemName: "magnifyingglass")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                TextField("Filter…", text: $filter.query)
                    .textFieldStyle(.plain)
                    .font(.system(.caption, design: .monospaced))
            }
            .padding(.horizontal, 7).padding(.vertical, 4)
            .background(Color.primary.opacity(0.06))
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .frame(maxWidth: 220)

            ForEach(LogLevel.allCases, id: \.self) { level in
                levelChip(level)
            }
            Spacer()
            Toggle("Auto-scroll", isOn: $autoScroll)
                .toggleStyle(.switch)
                .controlSize(.mini)
                .font(.caption)
        }
    }

    private func levelChip(_ level: LogLevel) -> some View {
        let isOn = filter.enabledLevels.contains(level)
        return Button {
            if isOn { filter.enabledLevels.remove(level) }
            else    { filter.enabledLevels.insert(level) }
        } label: {
            Text(level.rawValue)
                .font(.system(size: 10, design: .monospaced).bold())
                .padding(.horizontal, 7).padding(.vertical, 3)
                .background(levelColor(level).opacity(isOn ? 0.22 : 0.07))
                .foregroundStyle(isOn ? levelColor(level) : .secondary)
                .clipShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(isOn ? "Hide \(level.rawValue) lines" : "Show \(level.rawValue) lines")
    }

    private var logStream: some View {
        let visible = filter.apply(to: model.lines)
        return ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    if visible.isEmpty {
                        Text(model.lines.isEmpty
                             ? "No log lines found."
                             : "No lines match the current filter.")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .padding(10)
                    }
                    ForEach(visible) { line in
                        Text(line.text)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(levelColor(line.level))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(line.id)
                    }
                }
                .padding(10)
            }
            .background(Color.primary.opacity(0.045))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .onChange(of: model.lines) { _, _ in
                scrollToEnd(proxy, visible: visible)
            }
            .onChange(of: autoScroll) { _, on in
                if on { scrollToEnd(proxy, visible: visible) }
            }
        }
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy, visible: [LogLine]) {
        guard autoScroll, let last = visible.last else { return }
        proxy.scrollTo(last.id, anchor: .bottom)
    }

    private func copyFiltered() {
        let text = filter.apply(to: model.lines).map(\.text).joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private func levelColor(_ level: LogLevel?) -> Color {
        switch level {
        case .error: return .red
        case .warn:  return .orange
        case .info:  return Color.primary.opacity(0.85)
        case nil:    return .secondary
        }
    }
```

(`levelColor(_:)` takes an Optional so both chips — always non-nil — and lines — possibly untagged — use it.)

- [ ] **Step 3: Build**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make build`
Expected: `BUILD SUCCEEDED`. (`LogLevel`, `LogLine`, `LogFilter` come from `import RoccoPulseCore`, already imported at the top of the file; `NSPasteboard` comes from the existing `import AppKit`.)

- [ ] **Step 4: Run core suite (regression)**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/amastro/Projects/telemetrify
git add menubar/RoccoPulseApp/Sources/LogInspectorView.swift
git commit -m "feat(pulse): log inspector gains level chips, search, auto-scroll, copy"
```

---

### Task 7: Final verification + install

**Files:** none new.

- [ ] **Step 1: Full test + release build**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make test && make release`
Expected: all tests PASS, `BUILD SUCCEEDED`.

- [ ] **Step 2: Install and eyeball against the spec**

Run: `cd /Users/amastro/Projects/telemetrify/menubar && make install`
Then open the menubar popover and verify, against `design-explorations/ai-pulse-design4-final.html`:
1. Every service card has identical width; the spotlight icon's x-position is identical on the rocco-agent (up) and vllm (down) rows.
2. The lifecycle icon (play/stop/restart) renders OUTSIDE the card in the 44pt gutter; rows with no applicable action show the dashed ghost at the same spot.
3. Clicking Start on vllm flips the gutter to a spinner without any card shifting.
4. GPU section shows KPI strip + 2×2 ring gauges; sparklines grow after a couple of polls.
5. Log inspector: lines are colored by level, chips toggle levels, search narrows, auto-scroll sticks to the tail, Copy puts filtered text on the clipboard.

- [ ] **Step 3: Commit anything outstanding (plan checkboxes)**

```bash
cd /Users/amastro/Projects/telemetrify
git add -A docs/superpowers/plans/2026-06-05-ai-pulse-dataviz-redesign.md
git commit -m "docs: check off AI-Pulse data-viz redesign plan"
```
