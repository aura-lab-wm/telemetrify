import Foundation

/// Compact model-name formatting for the vllm row's in-card picker chip
/// (design Option B: design-explorations/vllm-model-picker-options.html).
/// The chip must hold a fixed-height single line, so long HF names
/// compress deterministically instead of mid-truncating: camel-case
/// words with ≥3 capitals become their initials ("WhiteRabbitNeo" →
/// "WRN"), boilerplate prefixes drop, and the leaf of an org/model
/// path is used. Lives in Core so the prober's summary line and the
/// view's chip share one rule (and one test suite).
public enum ModelChip {
    public static func label(model: String, precision: String? = nil) -> String {
        var leaf = model.split(separator: "/").last.map(String.init) ?? model
        for prefix in ["Meta-", "Llama-3.1-", "Llama-3-"] {
            leaf = leaf.replacingOccurrences(of: prefix, with: "")
        }
        let compressed = leaf.split(separator: "-").map { token -> String in
            let caps = token.filter(\.isUppercase)
            return caps.count >= 3 ? String(caps) : String(token)
        }.joined(separator: "-")
        guard let precision, !precision.isEmpty else { return compressed }
        return "\(compressed) · \(precision.uppercased())"
    }

    /// Compress model names that appear inside free text (e.g. the tier
    /// reason "Llama-3.1-WhiteRabbitNeo-2-70B BF16 (4 GPUs) — tool-capable").
    /// A token is treated as a model name when it's hyphenated AND one of
    /// its segments is camel-case (≥3 capitals) — plain prose like
    /// "tool-capable" or "Anthropic API" passes through untouched.
    public static func compressModelNames(in text: String) -> String {
        text.split(separator: " ", omittingEmptySubsequences: false)
            .map { token -> String in
                let t = String(token)
                guard t.contains("-"),
                      t.split(separator: "-").contains(where: {
                          $0.filter(\.isUppercase).count >= 3
                      })
                else { return t }
                return label(model: t)
            }
            .joined(separator: " ")
    }
}
