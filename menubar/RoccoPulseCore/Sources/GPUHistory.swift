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
