"""Per-turn auto-grader. LLM-as-Judge scores quality / hallucination /
completeness / refusal / followed_request and writes to `auto_grades`.

Two entry points:
  - grade_turn(conn, turn_id)         — single turn, used inline from capture.py
  - main()                            — bin/grade CLI for batch backfill
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

from . import prompts as P
from . import schemas as S
from .client import AnthropicClient, BudgetExceeded
from ..db import connect
from ..raw_archive import compress


def _ctx_for_turn(conn: sqlite3.Connection, turn_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, user_text, assistant_text, model, tool_call_count,
               attribution_skill
        FROM turns WHERE id = ?
        """,
        (turn_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "user_text":          (row["user_text"] or "").strip(),
        "assistant_text":     (row["assistant_text"] or "").strip()[:4000],
        "model":              row["model"] or "—",
        "tool_call_count":    int(row["tool_call_count"] or 0),
        "attribution_skill":  row["attribution_skill"] or "—",
    }


def grade_turn(
    conn: sqlite3.Connection,
    turn_id: int,
    *,
    override_budget_usd: float | None = None,
    skip_if_exists: bool = True,
    timeout: float = 10.0,
) -> dict | None:
    """Grade a single turn. Returns the persisted row dict, or None on skip/failure.
    Designed to be called inline from capture.py — never raises into the caller.
    """
    if skip_if_exists:
        if conn.execute("SELECT 1 FROM auto_grades WHERE turn_id = ?", (turn_id,)).fetchone():
            return None

    ctx = _ctx_for_turn(conn, turn_id)
    if not ctx:
        return None
    if not ctx["user_text"] or not ctx["assistant_text"]:
        return None

    client = AnthropicClient(conn, override_budget_usd=override_budget_usd)
    try:
        result = client.call(
            feature="grader",
            template=P.GRADER,
            user_kwargs=ctx,
            schema=S.GRADER,
            target_id=turn_id,
            max_tokens=400,
            timeout=timeout,
        )
    except BudgetExceeded:
        return None
    except Exception:
        return None

    parsed = result.parsed
    raw_blob = compress(result.raw_text)
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO auto_grades(
                turn_id, quality, hallucination, completeness, refusal,
                followed_request, notes, model, prompt_version,
                generated_at, cost_usd, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                int(parsed["quality"]),
                str(parsed["hallucination"]),
                int(parsed["completeness"]),
                1 if parsed["refusal"] else 0,
                int(parsed["followed_request"]),
                str(parsed.get("notes") or "")[:240],
                result.model,
                result.prompt_version,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                result.cost_usd,
                raw_blob,
            ),
        )
    return parsed


def _eligible_turn_ids(
    conn: sqlite3.Connection,
    *,
    limit: int | None,
    since_id: int | None,
    only_unscored: bool,
) -> list[int]:
    where = ["t.user_text IS NOT NULL", "t.assistant_text IS NOT NULL",
             "length(t.user_text) > 0", "length(t.assistant_text) > 0"]
    params: list[Any] = []
    if only_unscored:
        where.append("g.turn_id IS NULL")
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
    return [r["id"] for r in conn.execute(sql, params).fetchall()]


def grade_batch(
    *,
    limit: int | None = None,
    since_id: int | None = None,
    only_unscored: bool = True,
    backfill_budget_usd: float | None = None,
    workers: int = 4,
    log=print,
) -> dict:
    """Grade many turns concurrently. Each worker opens its own connection."""
    conn0 = connect()
    ids = _eligible_turn_ids(
        conn0, limit=limit, since_id=since_id, only_unscored=only_unscored
    )
    conn0.close()
    if not ids:
        log("nothing to grade.")
        return {"requested": 0, "graded": 0, "skipped": 0, "failed": 0, "cost_usd": 0.0}

    log(f"grading {len(ids)} turns (workers={workers}, "
        f"budget={'override $' + str(backfill_budget_usd) if backfill_budget_usd else 'daily cap'})")

    started = time.monotonic()
    graded = skipped = failed = 0

    def _one(turn_id: int) -> tuple[int, str]:
        c = connect()
        try:
            r = grade_turn(c, turn_id, override_budget_usd=backfill_budget_usd)
            if r is None:
                return turn_id, "skip"
            return turn_id, "ok"
        except Exception as e:
            return turn_id, f"err:{e!r}"
        finally:
            c.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (tid, outcome) in enumerate(ex.map(_one, ids), 1):
            if outcome == "ok":
                graded += 1
            elif outcome == "skip":
                skipped += 1
            else:
                failed += 1
            if i % 25 == 0 or i == len(ids):
                elapsed = time.monotonic() - started
                rate = i / elapsed if elapsed > 0 else 0.0
                log(f"  {i}/{len(ids)}  ok={graded} skip={skipped} fail={failed}  "
                    f"{rate:.1f}/s  eta={(len(ids)-i)/max(rate,1e-9):.0f}s")

    # Roll up cost from ai_runs.
    c2 = connect()
    cost_row = c2.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS s FROM ai_runs WHERE feature='grader' "
        "AND date(started_at)=date('now')"
    ).fetchone()
    cost = float(cost_row["s"])
    c2.close()

    summary = {
        "requested": len(ids),
        "graded": graded, "skipped": skipped, "failed": failed,
        "cost_usd_today_grader": cost,
        "duration_s": round(time.monotonic() - started, 1),
    }
    log(f"done: {summary}")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bin/grade")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--since-id", type=int, default=None)
    p.add_argument("--all", action="store_true",
                   help="also re-grade turns that already have a row")
    p.add_argument("--backfill-budget", type=float, default=None,
                   help="USD override for daily cap (e.g. 20.00 for full backfill)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    summary = grade_batch(
        limit=args.limit,
        since_id=args.since_id,
        only_unscored=not args.all,
        backfill_budget_usd=args.backfill_budget,
        workers=args.workers,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
