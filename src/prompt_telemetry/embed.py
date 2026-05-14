from functools import lru_cache

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL_NAME)


def embed(text: str) -> list[float]:
    vec = _model().encode(text, normalize_embeddings=True, convert_to_numpy=True)
    return vec.tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    arr = _model().encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                          show_progress_bar=False, batch_size=32)
    return [row.tolist() for row in arr]


def embed_turn(user_text: str, assistant_text: str) -> list[float]:
    """Embedding used for full-turn similarity search."""
    payload = f"PROMPT: {user_text.strip()}\n\nRESPONSE: {assistant_text.strip()[:4000]}"
    return embed(payload)


def embed_prompt(user_text: str) -> list[float]:
    """Embedding used for prompt-only similarity (paraphrase detection, clustering).
    No PROMPT:/RESPONSE: prefix, so it's directly comparable to other prompt embeddings."""
    return embed(user_text.strip())
