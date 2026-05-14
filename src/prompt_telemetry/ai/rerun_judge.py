"""Compare a rerun's response against the original turn and persist a verdict
(better / same / worse / inconclusive) plus per-dimension scores.

Called from rerun.run_rerun after the subprocess completes successfully.
"""
from __future__ import annotations

import json
import sqlite3

from . import prompts as P, schemas as S
from .client import AnthropicClient, BudgetExceeded
from ..raw_archive import compress


def _ctx_for_rerun(conn: sqlite3.Connection, rerun_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT r.id AS rerun_id, r.original_turn_id, r.model AS b_model,
               r.run_at AS b_date, r.response_text AS b_text,
               t.user_text, t.assistant_text AS a_text, t.model AS a_model,
               t.started_at AS a_date
        FROM reruns r JOIN turns t ON t.id = r.original_turn_id
        WHERE r.id = ?
        """,
        (rerun_id,),
    ).fetchone()
    if not row:
        return None
    if not row["b_text"] or not row["a_text"]:
        return None
    return {
        "user_text": (row["user_text"] or "").strip(),
        "a_text":    (row["a_text"] or "").strip()[:3000],
        "a_model":   row["a_model"] or "—",
        "a_date":    (row["a_date"] or "")[:19],
        "b_text":    (row["b_text"] or "").strip()[:3000],
        "b_model":   row["b_model"] or "—",
        "b_date":    (row["b_date"] or "")[:19],
    }


def judge(conn: sqlite3.Connection, rerun_id: int, *,
          override_budget_usd: float | None = None,
          skip_if_exists: bool = True) -> dict | None:
    """Judge a rerun. Returns the persisted row dict or None. Fail-silent."""
    if skip_if_exists:
        if conn.execute("SELECT 1 FROM rerun_judgments WHERE rerun_id = ?", (rerun_id,)).fetchone():
            return None
    ctx = _ctx_for_rerun(conn, rerun_id)
    if not ctx:
        return None

    client = AnthropicClient(conn, override_budget_usd=override_budget_usd)
    try:
        res = client.call(
            feature="rerun_judge",
            template=P.RERUN_JUDGE,
            user_kwargs=ctx,
            schema=S.RERUN_JUDGE,
            target_id=rerun_id,
            max_tokens=600,
            timeout=30.0,
        )
    except BudgetExceeded:
        return None
    except Exception:
        return None

    p = res.parsed
    dims_json = json.dumps(p.get("dimensions") or {}, ensure_ascii=False)
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO rerun_judgments(
                rerun_id, verdict, confidence, reasoning,
                dimensions_json, model, prompt_version, cost_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rerun_id,
                str(p["verdict"]),
                float(p["confidence"]),
                str(p.get("reasoning") or "")[:400],
                compress(dims_json),
                res.model,
                res.prompt_version,
                res.cost_usd,
            ),
        )
    return {
        "rerun_id": rerun_id,
        "verdict": p["verdict"],
        "confidence": p["confidence"],
        "reasoning": p.get("reasoning"),
        "dimensions": p.get("dimensions"),
        "cost_usd": res.cost_usd,
    }
