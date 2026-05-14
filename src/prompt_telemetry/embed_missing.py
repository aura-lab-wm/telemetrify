"""Embed any turn that doesn't yet have a row in turn_vec.

Run after bulk-ingest (`backfill --no-embed`) to populate the vector index.
Safe to interrupt and resume — already-embedded turns are skipped.

Usage:
    python -m prompt_telemetry.embed_missing             # all missing
    python -m prompt_telemetry.embed_missing --limit 500 # cap this run
    python -m prompt_telemetry.embed_missing --batch 32  # batch size
"""
import argparse
import sys
import time
from datetime import datetime, timezone

from .db import connect, serialize_embedding


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="cap embeddings this run (0 = no cap)")
    p.add_argument("--batch", type=int, default=32, help="embedding batch size")
    args = p.parse_args(argv)

    from .embed import _model  # lazy import to avoid loading torch unless needed
    model = _model()

    conn = connect()
    sql = """
        SELECT t.id, t.user_text, t.assistant_text
        FROM turns t LEFT JOIN turn_vec v ON v.turn_id = t.id
        WHERE v.turn_id IS NULL
        ORDER BY t.id ASC
    """
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()
    total = len(rows)
    if total == 0:
        print("nothing to embed.")
        return 0
    print(f"embedding {total} turns in batches of {args.batch}...")

    started = time.monotonic()
    done = 0
    for i in range(0, total, args.batch):
        batch = rows[i : i + args.batch]
        payloads = [
            f"PROMPT: {r['user_text'].strip()}\n\nRESPONSE: {r['assistant_text'].strip()[:4000]}"
            for r in batch
        ]
        vecs = model.encode(payloads, normalize_embeddings=True, convert_to_numpy=True,
                            show_progress_bar=False)
        with conn:
            for row, vec in zip(batch, vecs):
                conn.execute(
                    "INSERT OR REPLACE INTO turn_vec(turn_id, embedding) VALUES (?, ?)",
                    (row["id"], serialize_embedding(vec.tolist())),
                )
        done += len(batch)
        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  {done}/{total}  rate={rate:.1f}/s  eta={eta:.0f}s", flush=True)

    print(f"done in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
