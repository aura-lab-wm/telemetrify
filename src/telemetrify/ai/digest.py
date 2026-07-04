"""Daily digest — 4-6 sentence summary of the day's activity.

Reads today's turns + auto_grades + clusters, calls sonnet, persists to
daily_digests. Optionally fires a web push notification.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

from . import prompts as P, schemas as S
from .client import AnthropicClient, BudgetExceeded
from ..db import connect
from ..raw_archive import compress


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_digests (
            date              TEXT PRIMARY KEY,
            summary           TEXT NOT NULL,
            top_clusters_json BLOB,
            regressions_json  BLOB,
            suggestions_json  BLOB,
            generated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            model             TEXT,
            cost_usd          REAL
        );
    """)
    conn.commit()


def _stats(conn: sqlite3.Connection, day: str) -> dict:
    base = conn.execute(
        """SELECT COUNT(*) AS turns,
                  COALESCE(SUM(input_tokens+output_tokens),0) AS tokens,
                  AVG(g.quality) AS avg_q
           FROM turns t LEFT JOIN auto_grades g ON g.turn_id=t.id
           WHERE date(t.started_at) = ?""", (day,),
    ).fetchone()
    # A plain LEFT JOIN against turn_followups fans out when a turn has more
    # than one follow-up row, inflating both COUNT(*) and the corrected sum.
    # An EXISTS correlated subquery counts each turn exactly once regardless
    # of how many turn_followups rows reference it.
    correction = conn.execute(
        """SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM turn_followups f WHERE f.prev_turn_id = t.id
                  ) THEN 1 ELSE 0 END) AS corrected
           FROM turns t
           WHERE date(t.started_at) = ?""", (day,),
    ).fetchone()
    top_clusters = [dict(r) for r in conn.execute(
        """SELECT pc.id, COALESCE(pc.auto_label, pc.label) AS label,
                  COUNT(tc.turn_id) AS hits
           FROM turn_cluster tc
           JOIN turns t ON t.id = tc.turn_id
           JOIN prompt_clusters pc ON pc.id = tc.cluster_id
           WHERE date(t.started_at) = ?
           GROUP BY pc.id ORDER BY hits DESC LIMIT 3""", (day,),
    ).fetchall()]
    regressions = [dict(r) for r in conn.execute(
        """SELECT t.id, g.quality, COALESCE(pc.auto_label, pc.label) AS cluster
           FROM turns t JOIN auto_grades g ON g.turn_id=t.id
           LEFT JOIN turn_cluster tc ON tc.turn_id=t.id
           LEFT JOIN prompt_clusters pc ON pc.id = tc.cluster_id
           WHERE date(t.started_at) = ? AND g.quality <= 2
           ORDER BY g.quality ASC LIMIT 5""", (day,),
    ).fetchall()]

    pct = (100.0 * (correction["corrected"] or 0) / correction["total"]) if (correction and correction["total"]) else 0.0
    return {
        "turns_today": int(base["turns"] or 0),
        "tokens_today": int(base["tokens"] or 0),
        "avg_quality": round(float(base["avg_q"]), 2) if base["avg_q"] is not None else "n/a",
        "correction_pct": round(pct, 1),
        "top_clusters": top_clusters,
        "regressions": regressions,
        "suggestions": regressions[:3],  # simple heuristic
    }


def generate(conn: sqlite3.Connection, day: str | None = None,
             *, notify_push: bool = False,
             override_budget_usd: float | None = None) -> dict | None:
    _ensure_table(conn)
    # turns.started_at is stored in UTC (see capture.py/backfill.py), and the
    # `date(t.started_at) = ?` filters below run against that UTC value, so
    # the default "today" must also be the UTC calendar date -- not the local
    # one -- or turns get misattributed to the wrong day's digest near
    # midnight in the operator's local timezone. This matches the UTC-day
    # convention used elsewhere (dashboard.js treats bare SQLite timestamps as
    # UTC; charts.py buckets with SQLite's date('now'), which is UTC).
    day = day or datetime.now(timezone.utc).date().isoformat()
    stats = _stats(conn, day)
    if stats["turns_today"] == 0:
        return None

    def _block(items, fmt):
        return "; ".join(fmt(i) for i in items) or "(none)"

    user_kwargs = {
        "date": day,
        "turns_today": stats["turns_today"],
        "tokens_today": stats["tokens_today"],
        "avg_quality": stats["avg_quality"],
        "correction_pct": stats["correction_pct"],
        "top_clusters_block": _block(stats["top_clusters"], lambda i: f"#{i['id']} '{i['label']}' (x{i['hits']})"),
        "regressions_block": _block(stats["regressions"], lambda i: f"turn #{i['id']} q={i['quality']} ({i['cluster'] or '—'})"),
        "suggestions_block": _block(stats["suggestions"], lambda i: f"turn #{i['id']} ({i['cluster'] or '—'})"),
    }

    client = AnthropicClient(conn, override_budget_usd=override_budget_usd)
    try:
        res = client.call(
            feature="digest",
            template=P.DIGEST,
            user_kwargs=user_kwargs,
            schema=S.DIGEST,
            target_id=day,
            max_tokens=900, timeout=45.0,
        )
    except BudgetExceeded:
        return None

    p = res.parsed
    summary = (p.get("summary") or "").strip()
    top = p.get("top_clusters") or stats["top_clusters"]
    regs = p.get("regressions") or stats["regressions"]
    sugs = p.get("suggestions") or stats["suggestions"]

    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO daily_digests(
                date, summary, top_clusters_json, regressions_json,
                suggestions_json, generated_at, model, cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (day, summary,
             compress(json.dumps(top)), compress(json.dumps(regs)),
             compress(json.dumps(sugs)),
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             res.model, res.cost_usd),
        )

    if notify_push:
        try:
            from ..push_notify import notify as _push_notify
            _push_notify(
                conn,
                title=f"Telemetry digest · {day}",
                body=summary[:240],
                url="/dashboard",
            )
        except Exception:
            pass

    return {"date": day, "summary": summary, "cost_usd": res.cost_usd}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bin/digest")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--notify", action="store_true",
                   help="fire web-push notification after generating")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    conn = connect()
    r = generate(conn, day=args.date, notify_push=args.notify)
    if not r:
        print("(no activity for that day)"); return 0
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"--- digest for {r['date']} (${r['cost_usd']:.4f}) ---")
        print(r["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
