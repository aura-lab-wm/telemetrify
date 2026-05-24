"""Populate prompt_vec for every existing turn that doesn't yet have one.

Embeds user_text only (no response prefix) — required for paraphrase detection
and prompt-only clustering.
"""
import sys
import time

from .db import connect, serialize_embedding


def main() -> int:
    conn = connect()
    rows = conn.execute(
        """
        SELECT t.id, t.user_text
        FROM turns t LEFT JOIN prompt_vec p ON p.turn_id = t.id
        WHERE p.turn_id IS NULL AND t.user_text IS NOT NULL
        ORDER BY t.id ASC
        """
    ).fetchall()
    if not rows:
        print("nothing to do.")
        return 0
    print(f"embedding {len(rows)} prompts…")
    from .embed import _model
    model = _model()

    batch_size = 64
    started = time.monotonic()
    done = 0
    total = len(rows)
    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        texts = [r["user_text"].strip() for r in batch]
        vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                            show_progress_bar=False, batch_size=batch_size)
        with conn:
            for row, vec in zip(batch, vecs):
                conn.execute(
                    "INSERT OR REPLACE INTO prompt_vec(turn_id, embedding) VALUES (?, ?)",
                    (row["id"], serialize_embedding(vec.tolist())),
                )
        done += len(batch)
        elapsed = time.monotonic() - started
        rate = done / max(elapsed, 1e-9)
        eta = (total - done) / max(rate, 1e-9)
        print(f"  {done}/{total}  rate={rate:.0f}/s  eta={eta:.0f}s", flush=True)

    print(f"done in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
