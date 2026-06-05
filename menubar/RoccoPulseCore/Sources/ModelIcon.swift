import Foundation

/// Maps a model name to the asset-catalog name of its vendor's ORIGINAL
/// avatar (fetched once from the HF org page into Assets.xcassets as
/// model-<vendor>). Name-based because the agent reports bare model
/// names with no org prefix. Order matters: finetune vendors
/// (WhiteRabbitNeo on a Llama base) are checked before base families.
public enum ModelIcon {
    private static let rules: [(needle: String, asset: String)] = [
        ("whiterabbitneo", "model-whiterabbitneo"),  // before llama: WRN finetunes
        ("kimi",           "model-moonshotai"),
        ("qwen",           "model-qwen"),
        ("qwq",            "model-qwen"),
        ("glm",            "model-zai"),
        ("deepseek",       "model-deepseek"),
        ("mistral",        "model-mistral"),
        ("llama",          "model-meta"),
        ("internvl",       "model-opengvlab"),
        ("dots.ocr",       "model-rednote"),
    ]

    public static func asset(for model: String) -> String? {
        let leaf = (model.split(separator: "/").last.map(String.init) ?? model)
            .lowercased()
        return rules.first { leaf.contains($0.needle) }?.asset
    }
}
