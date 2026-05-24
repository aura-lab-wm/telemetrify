"""Detect retry / correction follow-ups within a session.

A turn is a follow-up of its immediate prior turn (same session) when *either*:
  (a) cosine distance between their prompt embeddings is < PARAPHRASE_THRESHOLD
  (b) the prompt opens with a corrective phrase (regex)
"""
from __future__ import annotations

import re
import sqlite3
import struct

from .db import serialize_embedding

PARAPHRASE_THRESHOLD = 0.40

CORRECTIVE_RE = re.compile(
    r"^\s*(no[,. ]|actually|instead|wait[,. ]|wrong|that's not|that isn't|don't|stop\b|undo\b|revert\b|nope|nah\b|never mind|cancel that|forget that)",
    re.IGNORECASE,
)


def _vec_to_floats(b: bytes) -> tuple[float, ...]:
    n = len(b) // 4
    return struct.unpack(f"{n}f", b)


def _cosine_dist(a: bytes, b: bytes) -> float:
    """Both inputs are normalized 384-dim float32 vectors (we always store normalized)."""
    av = _vec_to_floats(a)
    bv = _vec_to_floats(b)
    dot = sum(x * y for x, y in zip(av, bv))
    return 1.0 - dot


def detect_for_turn(conn: sqlite3.Connection, turn_id: int) -> dict | None:
    """Compute and persist a follow-up record for `turn_id` if applicable.
    Returns the inserted/updated record, or None if no follow-up detected.
    """
    cur = conn.execute(
        """
        SELECT t.id, t.session_id, t.started_at, t.user_text
        FROM turns t WHERE t.id = ?
        """,
        (turn_id,),
    ).fetchone()
    if not cur:
        return None

    prev = conn.execute(
        """
        SELECT id, user_text FROM turns
        WHERE session_id = ? AND started_at < ? AND origin = 'organic'
        ORDER BY started_at DESC LIMIT 1
        """,
        (cur["session_id"], cur["started_at"]),
    ).fetchone()
    if not prev:
        return None

    text = (cur["user_text"] or "").strip()
    corrective = bool(CORRECTIVE_RE.match(text))

    distance: float | None = None
    cur_vec = conn.execute("SELECT embedding FROM prompt_vec WHERE turn_id = ?", (cur["id"],)).fetchone()
    prev_vec = conn.execute("SELECT embedding FROM prompt_vec WHERE turn_id = ?", (prev["id"],)).fetchone()
    if cur_vec and prev_vec:
        distance = _cosine_dist(cur_vec["embedding"], prev_vec["embedding"])
    paraphrase = distance is not None and distance < PARAPHRASE_THRESHOLD

    if not corrective and not paraphrase:
        return None

    kind = "both" if (corrective and paraphrase) else ("corrective" if corrective else "paraphrase")
    reason_bits = []
    if corrective:
        reason_bits.append(f"matches /{CORRECTIVE_RE.pattern[:50]}…/")
    if paraphrase:
        reason_bits.append(f"prompt-distance={distance:.3f}")
    reason = "; ".join(reason_bits)

    conn.execute(
        """
        INSERT INTO turn_followups(turn_id, prev_turn_id, kind, reason, distance)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(turn_id) DO UPDATE SET
            prev_turn_id = excluded.prev_turn_id,
            kind         = excluded.kind,
            reason       = excluded.reason,
            distance     = excluded.distance,
            detected_at  = datetime('now')
        """,
        (cur["id"], prev["id"], kind, reason, distance),
    )
    return {"turn_id": cur["id"], "prev_turn_id": prev["id"], "kind": kind, "reason": reason, "distance": distance}


def backfill(conn: sqlite3.Connection, log=print) -> int:
    """Compute follow-ups across the entire corpus. Idempotent (UPSERT)."""
    sessions = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT session_id FROM turns ORDER BY started_at ASC"
    ).fetchall()]
    total = 0
    for i, sid in enumerate(sessions, 1):
        turns = conn.execute(
            "SELECT id FROM turns WHERE session_id = ? AND origin = 'organic' ORDER BY started_at ASC",
            (sid,),
        ).fetchall()
        if len(turns) < 2:
            continue
        with conn:
            for row in turns[1:]:
                rec = detect_for_turn(conn, row["id"])
                if rec:
                    total += 1
        if i % 50 == 0:
            log(f"  session {i}/{len(sessions)}: total followups so far = {total}")
    log(f"backfill complete: {total} follow-ups recorded")
    return total
