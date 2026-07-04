import threading

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Lazy-loaded singleton, guarded by double-checked locking. A bare
# @lru_cache(maxsize=1) is NOT safe here: two threads racing to call
# `_model()` before the cache is populated can both pass the (unlocked)
# "is it cached yet" check and both construct a SentenceTransformer
# concurrently — wasted work at best, and a source of hangs/contention
# inside the underlying torch/transformers load at worst. The lock below
# ensures only the first caller ever constructs the model; every other
# caller (racing or not) blocks briefly on the lock and then reads the
# already-populated cache.
_model_lock = threading.Lock()
_model_instance = None


def _model():
    global _model_instance
    if _model_instance is None:
        with _model_lock:
            if _model_instance is None:
                from sentence_transformers import SentenceTransformer
                _model_instance = SentenceTransformer(MODEL_NAME)
    return _model_instance


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
