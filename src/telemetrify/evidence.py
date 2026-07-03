"""Evidence-backing grade — the "unsupported-claim rate" metric (#2).

For each turn whose assistant_text asserts success/completion (configurable
claim lexicon), check whether the SAME turn's tool_calls contain a captured
tool RESULT (non-read-only) that backs the claim. A success claim with no
supporting tool output → `unsupported_claim` (evidence_backed = 0).

Stored as the `evidence_backed` dimension on `auto_grades` so it trends in
the existing grading surface:
  1 = a success claim is backed by a tool result
  0 = a success claim is present but no tool result backs it (unsupported)
  NULL = no success claim was made / not assessable

The heuristic is OUTPUT-disciplined and read-only-tool-disciplined so it
doesn't overfire: a success marker inside source code the agent merely READ
cannot back a claim (see telemetrify.outcome_rules.evidence_assess).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import outcome_rules


def _project_from_cwd(cwd: str | None) -> str:
    return (cwd or "").strip()


def _load_turn_context(conn: sqlite3.Connection, turn_id: int) -> dict | None:
    row = conn.execute(
        "SELECT assistant_text, cwd FROM turns WHERE id = ?", (turn_id,)
    ).fetchone()
    if not row:
        return None
    tcs = conn.execute(
        "SELECT tool_name, output_text FROM tool_calls WHERE turn_id = ?",
        (turn_id,),
    ).fetchall()
    return {
        "assistant_text": row["assistant_text"] or "",
        "project": _project_from_cwd(row["cwd"]),
        "tool_results": [(r["tool_name"], r["output_text"] or "") for r in tcs],
    }


def assess_turn(conn: sqlite3.Connection, turn_id: int) -> int | None:
    """Assess one turn and persist evidence_backed on auto_grades. Returns
    the value written (1/0/None). Idempotent — safe to re-run.

    If an auto_grades row already exists (from the LLM grader), only the
    evidence_backed column is touched (INSERT OR REPLACE would clobber the
    grader's scores, so we UPDATE-or-INSERT just this dimension)."""
    ctx = _load_turn_context(conn, turn_id)
    if not ctx:
        return None
    claim_present, backed = outcome_rules.evidence_assess(
        ctx["project"], ctx["assistant_text"], ctx["tool_results"]
    )
    if not claim_present:
        value = None
    else:
        value = 1 if backed else 0

    with conn:
        exists = conn.execute(
            "SELECT 1 FROM auto_grades WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE auto_grades SET evidence_backed = ? WHERE turn_id = ?",
                (value, turn_id),
            )
        else:
            # No LLM grade yet — seed a row holding only this dimension so
            # the unsupported-claim rate is queryable before the grader
            # runs. The grader's INSERT OR REPLACE preserves this column
            # via the COALESCE in its upsert (see ai/grader.py).
            conn.execute(
                """
                INSERT OR REPLACE INTO auto_grades(
                    turn_id, evidence_backed, generated_at, prompt_version
                ) VALUES (?, ?, ?, 'evidence-v1')
                """,
                (turn_id, value, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
    return value


def backfill(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    since_id: int | None = None,
    only_unscored: bool = True,
    log=print,
) -> dict:
    """Assess evidence-backing for many turns. `only_unscored` skips turns
    that already have a non-NULL evidence_backed."""
    where = ["assistant_text IS NOT NULL", "length(assistant_text) > 0"]
    params: list = []
    if only_unscored:
        where.append("(evidence_backed IS NULL OR evidence_backed IS NULL)")
    if since_id is not None:
        where.append("t.id > ?")
        params.append(since_id)
    sql = (
        "SELECT t.id FROM turns t "
        "LEFT JOIN auto_grades g ON g.turn_id = t.id "
        f"WHERE {' AND '.join(where)} ORDER BY t.id ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    ids = [r["id"] for r in conn.execute(sql, params).fetchall()]
    if not ids:
        log("nothing to assess.")
        return {"assessed": 0, "unsupported": 0, "backed": 0, "no_claim": 0}

    assessed = unsupported = backed = no_claim = 0
    for i, tid in enumerate(ids, 1):
        v = assess_turn(conn, tid)
        assessed += 1
        if v is None:
            no_claim += 1
        elif v == 1:
            backed += 1
        else:
            unsupported += 1
        if i % 500 == 0 or i == len(ids):
            log(f"  evidence {i}/{len(ids)}  unsupported={unsupported} "
                f"backed={backed} no_claim={no_claim}")
    return {
        "assessed": assessed,
        "unsupported": unsupported,
        "backed": backed,
        "no_claim": no_claim,
    }


# ─── Aggregate recipes ───────────────────────────────────────────────────

def unsupported_claim_rate(conn: sqlite3.Connection) -> dict:
    """Overall unsupported-claim rate = unsupported / (unsupported + backed)
    over turns that asserted a claim. Turns with no claim are excluded."""
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN evidence_backed = 0 THEN 1 ELSE 0 END) AS unsupported,
          SUM(CASE WHEN evidence_backed = 1 THEN 1 ELSE 0 END) AS backed,
          SUM(CASE WHEN evidence_backed IS NULL THEN 1 ELSE 0 END) AS no_claim
        FROM auto_grades
        """
    ).fetchone()
    unsupported = int(row["unsupported"] or 0)
    backed = int(row["backed"] or 0)
    no_claim = int(row["no_claim"] or 0)
    asserted = unsupported + backed
    return {
        "unsupported": unsupported,
        "backed": backed,
        "no_claim": no_claim,
        "rate": (unsupported / asserted) if asserted else None,
    }


def unsupported_trend(conn: sqlite3.Connection, *, bucket: str = "week") -> list[dict]:
    """Per-bucket unsupported-claim rate over turns that asserted a claim."""
    fmt = "%Y-W%W" if bucket == "week" else "%Y-%m-%d"
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', t.started_at) AS b,
               SUM(CASE WHEN g.evidence_backed = 0 THEN 1 ELSE 0 END) AS unsupported,
               SUM(CASE WHEN g.evidence_backed = 1 THEN 1 ELSE 0 END) AS backed
        FROM auto_grades g
        JOIN turns t ON t.id = g.turn_id
        WHERE g.evidence_backed IS NOT NULL
          AND t.started_at IS NOT NULL
        GROUP BY b
        ORDER BY b
        """
    ).fetchall()
    out = []
    for r in rows:
        asserted = int(r["unsupported"] or 0) + int(r["backed"] or 0)
        out.append({
            "bucket": r["b"],
            "unsupported": int(r["unsupported"] or 0),
            "backed": int(r["backed"] or 0),
            "rate": (int(r["unsupported"] or 0) / asserted) if asserted else 0.0,
        })
    return out


def per_session(conn: sqlite3.Connection) -> list[dict]:
    """Unsupported-claim rate per session."""
    rows = conn.execute(
        """
        SELECT t.session_id,
               SUM(CASE WHEN g.evidence_backed = 0 THEN 1 ELSE 0 END) AS unsupported,
               SUM(CASE WHEN g.evidence_backed = 1 THEN 1 ELSE 0 END) AS backed
        FROM auto_grades g
        JOIN turns t ON t.id = g.turn_id
        WHERE g.evidence_backed IS NOT NULL
        GROUP BY t.session_id
        HAVING unsupported + backed > 0
        ORDER BY (unsupported * 1.0 / (unsupported + backed)) DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        u = int(r["unsupported"] or 0)
        b = int(r["backed"] or 0)
        out.append({
            "session_id": r["session_id"],
            "unsupported": u,
            "backed": b,
            "rate": (u / (u + b)) if (u + b) else 0.0,
        })
    return out