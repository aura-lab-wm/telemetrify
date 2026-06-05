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
        let util = GPUSummary(gpus: snap.gpus).map { "\($0.avgUtilPct)%" }
        if snap.vllm.running {
            let model = ModelChip.label(model: snap.vllm.model ?? "vLLM")
            return [model, util].compactMap { $0 }.joined(separator: " ")
        }
        return util
    }
}
