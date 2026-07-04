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
# Deeper candidate pool when a filter is active: the filter is applied to the
# candidate set, so a shallow pool can come back empty for selective filters.
FILTERED_FANOUT = 1000

# sqlite3 binds Python ints as signed 64-bit SQLite INTEGERs; anything outside
# this range raises an unhandled OverflowError at bind time (not at parse
# time), which previously surfaced as a raw HTTP 500 for a query param like
# `min_tokens=99999999999999999999999999`. Clamp to this range up front so an
# out-of-range value is treated the same as any other unparseable value.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1

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


def _escape_like(value: str, escape_char: str = "\\") -> str:
    """Escape SQL LIKE metacharacters (`%`, `_`) in a user-supplied value so
    they match literally instead of acting as wildcards. Must be paired with
    an `ESCAPE '<escape_char>'` clause on the LIKE itself. The escape
    character must be escaped first so a literal backslash in the input
    isn't mistaken for an escape sequence."""
    return (
        value.replace(escape_char, escape_char * 2)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
    )


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
        try:
            v = int(s)
        except (TypeError, ValueError):
            return None
        if v < _SQLITE_INT_MIN or v > _SQLITE_INT_MAX:
            # Out of SQLite's bindable range: drop the filter rather than let
            # it reach `conn.execute(...)` and raise OverflowError.
            return None
        return v

    def _d(s: str) -> str | None:
        if not s: return None
        try: datetime.fromisoformat(s); return s
        except ValueError: return None

    if (v := raw.get("model")):
        clauses.append("t.model = ?"); params.append(v)
    if (v := raw.get("cwd_glob")):
        # `*` is this app's own glob wildcard syntax; escape any literal SQL
        # LIKE metacharacters (%, _) FIRST so they match literally, then turn
        # `*` into the real SQL wildcard. Without this a cwd containing a
        # literal "%" (or "_") would match far more rows than intended.
        clauses.append("t.cwd LIKE ? ESCAPE '\\'")
        params.append(_escape_like(v).replace("*", "%"))
    if (v := raw.get("skill")):
        clauses.append("t.attribution_skill = ?"); params.append(v)
    if (v := _i(raw.get("cluster", ""))) is not None:
        # `is not None`, not truthiness: cluster_id 0 is a real cluster.
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

    # Apply filters INSIDE the candidate CTEs, not after fusion. Previously the
    # vec/FTS sides each pulled their top-`fanout` purely by similarity and the
    # filter was applied afterward — so a filtered search returned EMPTY whenever
    # none of the unfiltered top-50 happened to match the filter, even though
    # matching turns existed deeper in the ranking. When filtering, we also pull
    # a much deeper candidate pool so selective filters still surface results.
    filtered = bool(filters.where)
    eff_fanout = max(fanout, FILTERED_FANOUT) if filtered else fanout
    and_where = (" AND " + filters.where) if filtered else ""
    sql = f"""
    WITH vec_rank AS (
      SELECT v.turn_id AS id, ROW_NUMBER() OVER (ORDER BY v.distance) AS r
      FROM turn_vec v
      JOIN turns t ON t.id = v.turn_id
      WHERE v.embedding MATCH ? AND v.k = ?{and_where}
    ),
    fts_rank AS (
      SELECT f.rowid AS id, ROW_NUMBER() OVER (ORDER BY f.rank) AS r
      FROM turns_fts f
      JOIN turns t ON t.id = f.rowid
      WHERE turns_fts MATCH ?{and_where}
      LIMIT ?
    ),
    fused AS (
      SELECT id, SUM(1.0/(? + r)) AS score
      FROM (SELECT * FROM vec_rank UNION ALL SELECT * FROM fts_rank)
      GROUP BY id
    )
    SELECT t.*, f.score AS score
    FROM fused f JOIN turns t ON t.id = f.id
    ORDER BY f.score DESC
    LIMIT ?
    """
    params = [qvec, eff_fanout, *filters.params,   # vec_rank (MATCH, k, filter)
              qfts, *filters.params, eff_fanout,    # fts_rank (MATCH, filter, LIMIT)
              RRF_K, k]                              # fused weight + final LIMIT
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # FTS5 may fail on pathological queries; fall back to vec-only.
        return _vec_only(conn, qvec, k, filters)
    return [dict(r) for r in rows]


def _vec_only(conn: sqlite3.Connection, qvec: bytes, k: int, filters: Filters) -> list[dict]:
    filtered = bool(filters.where)
    # Deepen the KNN pool when filtering so selective filters don't come back
    # empty (the candidate set is filtered, then ranked).
    eff_k = max(k, FILTERED_FANOUT) if filtered else max(k, 50)
    where = ("AND " + filters.where) if filtered else ""
    sql = f"""
    SELECT t.*, v.distance AS score
    FROM turn_vec v JOIN turns t ON t.id = v.turn_id
    WHERE v.embedding MATCH ? AND v.k = ?
    {where}
    ORDER BY v.distance ASC
    LIMIT ?
    """
    params = [qvec, eff_k, *filters.params, k]
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
        # Pull a deep KNN pool BEFORE excluding self + same-session: a tight
        # cluster of same-session neighbors would otherwise crowd out every
        # cross-session match and we'd return far fewer than k (the old `k + 5`
        # could return zero for a chatty session).
        (row["embedding"], max(k * 8, 64), row["session_id"], turn_id, k),
    ).fetchall()
    return [dict(r) for r in rows]
