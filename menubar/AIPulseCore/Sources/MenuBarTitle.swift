import Foundation

/// The menubar label's text — the bolt alone said nothing ("only the
/// thunder feels silly" — the operator). When the snapshot is FRESH:
///
///   vLLM serving  → "WRN-2-70B 97%"   (abbreviated model + avg util)
///   idle          → "3%"              (avg util keeps the rig honest)
///
/// Stale or missing snapshots return nil (icon-only): a number we can't
/// trust is worse than no number, and the dimmed mark already signals
/// the outage.
public enum MenuBarTitle {
    public static func make(snapshot: RoccoStatus?, now: Date) -> String? {
        guard let snap = snapshot, !snap.isStale(now: now) else { return nil }
        if snap.vllm.running {
            let model = ModelChip.label(model: snap.vllm.model ?? "vLLM")
            // WORKING: tokens are flowing → show throughput, the live
            // "model is doing something" signal (▸ <n> tok/s). Idle but
            // up → fall back to GPU util so the rig still reads honest.
            if let inf = snap.inferenceRecent, inf.isWorking {
                return "\(model) \u{25B8} \(Int(inf.tokensPerSec.rounded())) tok/s"
            }
            let util = GPUSummary(gpus: snap.gpus).map { " \($0.avgUtilPct)%" } ?? ""
            return model + util
        }
        return GPUSummary(gpus: snap.gpus).map { "\($0.avgUtilPct)%" }
    }
}
