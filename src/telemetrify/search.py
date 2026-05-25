"""Hybrid retrieval over turns: FTS5 BM25 ∪ sqlite-vec cosine, fused via RRF.

Plus a defensive filter-bar parser used by the UI.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .db import serialize_embedding
from .embed import embed

RRF_K = 60          # standard RRF smoothing constant
DEFAULT_FANOUT = 50  # candidates pulled from each side before fusion

# Whitelisted filter param names; anything else is rejected.
ALLOWED_FILTERS = {
    "model", "cwd_glob", "skill", "cluster", "origin",
    "since", "until",
    "has_error", "has_followup", "has_annotation", "tag",
    "min_tokens", "max_tokens",
    "min_latency_ms", "max_latency_ms",
}


@dataclass
class Filters:
    where: str = ""
    params: list[Any] = field(default_factory=list)


def _quote_fts(q: str) -> str:
    """FTS5 MATCH expects a query string. Defensive escape: collapse quotes and
    wrap each whitespace-separated token in double quotes so punctuation/colons
    inside file paths don't fail the parser."""
    tokens = [t.strip().strip('"').strip("'") for t in q.split() if t.strip()]
    return " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens if t) or '""'


def parse_filters(raw: dict[str, str]) -> Filters:
    """Build a SQL WHERE fragment (without the WHERE keyword) and a bound-params
    list from a user-supplied filter dict. Silently drops unknown keys.
    """
    clauses: list[str] = []
    params: list[Any] = []

    def _i(s: str) -> int | None:
        try: return int(s)
        except (TypeError, ValueError): return None

    def _d(s: str) -> str | None:
        if not s: return None
        try: datetime.fromisoformat(s); return s
        except ValueError: return None

    if (v := raw.get("model")):
        clauses.append("t.model = ?"); params.append(v)
    if (v := raw.get("cwd_glob")):
        clauses.append("t.cwd LIKE ?"); params.append(v.replace("*", "%"))
    if (v := raw.get("skill")):
        clauses.append("t.attribution_skill = ?"); params.append(v)
    if (v := _i(raw.get("cluster", ""))):
        clauses.append("EXISTS (SELECT 1 FROM turn_cluster tc WHERE tc.turn_id = t.id AND tc.cluster_id = ?)")
        params.append(v)
    if (v := raw.get("origin")) in ("organic", "rerun"):
        clauses.append("t.origin = ?"); params.append(v)
    if (v := _d(raw.get("since", ""))):
        clauses.append("t.started_at >= ?"); params.append(v)
    if (v := _d(raw.get("until", ""))):
        clauses.append("t.started_at < ?"); params.append(v)
    if raw.get("has_error") == "1":
        clauses.append("EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.turn_id = t.id AND tc.is_error = 1)")
    if raw.get("has_followup") == "1":
        clauses.append("EXISTS (SELECT 1 FROM turn_followups f WHERE f.turn_id = t.id)")
    if raw.get("has_annotation") == "1":
        clauses.append("EXISTS (SELECT 1 FROM annotations a WHERE a.turn_id = t.id)")
    if (v := raw.get("tag")):
        # Whole-element CSV membership, space-tolerant: a turn matches if any
        # annotation's `tags` (CSV) contains this tag as a full element. Lets a
        # curated workspace (e.g. seminar-coding-agents) get its own clean view.
        clauses.append(
            "EXISTS (SELECT 1 FROM annotations a WHERE a.turn_id = t.id "
            "AND (',' || REPLACE(COALESCE(a.tags,''), ' ', '') || ',') LIKE ?)"
        )
        params.append("%," + v.replace(" ", "") + ",%")
    if (v := _i(raw.get("min_tokens", ""))) is not None:
        clauses.append("(COALESCE(t.input_tokens,0)+COALESCE(t.output_tokens,0)) >= ?")
        params.append(v)
    if (v := _i(raw.get("max_tokens", ""))) is not None:
        clauses.append("(COALESCE(t.input_tokens,0)+COALESCE(t.output_tokens,0)) <= ?")
        params.append(v)
    if (v := _i(raw.get("min_latency_ms", ""))) is not None:
        clauses.append("t.latency_ms >= ?"); params.append(v)
    if (v := _i(raw.get("max_latency_ms", ""))) is not None:
        clauses.append("t.latency_ms <= ?"); params.append(v)

    return Filters(where=" AND ".join(clauses), params=params)


def hybrid_search(
    conn: sqlite3.Connection,
    q: str,
    k: int = 20,
    filters: Filters | None = None,
    fanout: int = DEFAULT_FANOUT,
) -> list[dict]:
    """Return the top-k turns ranked by RRF over FTS5 BM25 + vec cosine."""
    filters = filters or Filters()
    qvec = serialize_embedding(embed(q))
    qfts = _quote_fts(q)

    where = ("WHERE " + filters.where) if filters.where else ""
    sql = f"""
    WITH vec_rank AS (
      SELECT turn_id AS id, ROW_NUMBER() OVER (ORDER BY distance) AS r
      FROM turn_vec WHERE embedding MATCH ? AND k = ?
    ),
    fts_rank AS (
      SELECT rowid AS id, ROW_NUMBER() OVER (ORDER BY rank) AS r
      FROM turns_fts WHERE turns_fts MATCH ? LIMIT ?
    ),
    fused AS (
      SELECT id, SUM(1.0/(? + r)) AS score
      FROM (SELECT * FROM vec_rank UNION ALL SELECT * FROM fts_rank)
      GROUP BY id
    )
    SELECT t.*, f.score AS score
    FROM fused f JOIN turns t ON t.id = f.id
    {where}
    ORDER BY f.score DESC
    LIMIT ?
    """
    params = [qvec, fanout, qfts, fanout, RRF_K, *filters.params, k]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # FTS5 may fail on pathological queries; fall back to vec-only.
        return _vec_only(conn, qvec, k, filters)
    return [dict(r) for r in rows]


def _vec_only(conn: sqlite3.Connection, qvec: bytes, k: int, filters: Filters) -> list[dict]:
    where = ("AND " + filters.where) if filters.where else ""
    sql = f"""
    SELECT t.*, v.distance AS score
    FROM turn_vec v JOIN turns t ON t.id = v.turn_id
    WHERE v.embedding MATCH ? AND v.k = ?
    {where}
    ORDER BY v.distance ASC
    LIMIT ?
    """
    params = [qvec, max(k, 50), *filters.params, k]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def recent(conn: sqlite3.Connection, k: int, filters: Filters | None = None) -> list[dict]:
    """Filtered recent turns (no search query)."""
    filters = filters or Filters()
    where = ("WHERE " + filters.where) if filters.where else ""
    rows = conn.execute(
        f"""
        SELECT t.*, NULL AS score
        FROM turns t
        {where}
        ORDER BY t.started_at DESC
        LIMIT ?
        """,
        (*filters.params, k),
    ).fetchall()
    return [dict(r) for r in rows]


def similar_turns(conn: sqlite3.Connection, turn_id: int, k: int = 5) -> list[dict]:
    """KNN-by-embedding, excluding self and same-session turns."""
    row = conn.execute("SELECT session_id, embedding FROM turn_vec v JOIN turns t ON t.id=v.turn_id WHERE t.id=?", (turn_id,)).fetchone()
    if not row:
        return []
    rows = conn.execute(
        """
        SELECT t.id, t.user_text, t.session_id, t.started_at, t.cwd, v.distance
        FROM turn_vec v JOIN turns t ON t.id = v.turn_id
        WHERE v.embedding MATCH ? AND v.k = ?
          AND t.session_id != ?
          AND t.id != ?
        ORDER BY v.distance ASC LIMIT ?
        """,
        (row["embedding"], k + 5, row["session_id"], turn_id, k),
    ).fetchall()
    return [dict(r) for r in rows]
