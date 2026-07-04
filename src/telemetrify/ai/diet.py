"""Prompt Diet Analyzer — propose tightened versions of frequent prompts.

Targets clusters with ≥5 members where avg auto-grade quality < 4 OR has high
follow-up rate. Persists to prompt_diet_suggestions for UI surfacing.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

from . import prompts as P, schemas as S
from .client import AnthropicClient, BudgetExceeded
from .cluster_label import _is_image_placeholder
from ..db import connect


def _ensure_table(conn: sqlite3.Connection) -> None:
    # Lazy-create the table so v3 can ship without yet-another full migration
    # round-trip; will be moved into migration 013 in a follow-up.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prompt_diet_suggestions (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id               INTEGER REFERENCES prompt_clusters(id) ON DELETE CASCADE,
            original_representative  TEXT,
            tightened_text           TEXT,
            predicted_savings_pct    REAL,
            reasoning                TEXT,
            accepted                 INTEGER DEFAULT 0,
            generated_at             TEXT NOT NULL DEFAULT (datetime('now')),
            model                    TEXT,
            cost_usd                 REAL
        );
        CREATE INDEX IF NOT EXISTS idx_diet_cluster ON prompt_diet_suggestions(cluster_id);
    """)
    conn.commit()


def _candidate_clusters(conn: sqlite3.Connection, *, min_members: int = 5,
                         max_quality: float = 4.0, limit: int = 20) -> list[dict]:
    """Clusters most worth dieting."""
    # turn_followups.turn_id identifies the *follow-up* turn itself; the turn
    # that got followed-up-on (i.e. the one whose prompt is worth dieting) is
    # referenced by turn_followups.prev_turn_id. Joining on f.turn_id (as this
    # used to) checks "is this member itself a follow-up to something else",
    # which inverts the intended "does this member get followed up on" signal.
    # An EXISTS correlated subquery (rather than a LEFT JOIN ... prev_turn_id)
    # also avoids fanning out turn_cluster rows when a turn has more than one
    # turn_followups row pointing at it.
    return [dict(r) for r in conn.execute(
        """
        SELECT pc.id, COALESCE(pc.auto_label, pc.label) AS label,
               pc.member_count, pc.representative_turn_id,
               AVG(g.quality) AS avg_quality,
               SUM(CASE WHEN EXISTS (
                     SELECT 1 FROM turn_followups f WHERE f.prev_turn_id = tc.turn_id
                   ) THEN 1 ELSE 0 END) * 1.0
                 / pc.member_count AS followup_rate
        FROM prompt_clusters pc
        JOIN turn_cluster tc ON tc.cluster_id = pc.id
        LEFT JOIN auto_grades g ON g.turn_id = tc.turn_id
        WHERE pc.member_count >= ?
        GROUP BY pc.id
        HAVING avg_quality IS NULL OR avg_quality < ? OR followup_rate > 0.1
        ORDER BY pc.member_count DESC
        LIMIT ?
        """, (min_members, max_quality, limit),
    ).fetchall()]


def _members(conn: sqlite3.Connection, cluster_id: int, k: int = 5) -> list[str]:
    # Mirrors cluster_label._members_for_cluster: walk each turn's lines for
    # the first one that isn't blank or a Claude Code image-paste placeholder
    # ("[Image: original WxH...]" / "[Image #N]") rather than blindly taking
    # splitlines()[0], which could surface that placeholder verbatim (or raise
    # IndexError on whitespace-only user_text).
    out = []
    for r in conn.execute(
        """SELECT t.user_text FROM turn_cluster tc JOIN turns t ON t.id = tc.turn_id
           WHERE tc.cluster_id = ? ORDER BY t.id DESC LIMIT ?""",
        (cluster_id, k),
    ).fetchall():
        for line in (r["user_text"] or "").strip().splitlines():
            line = line.strip()
            if not line or _is_image_placeholder(line):
                continue
            out.append(line[:280])
            break
    return out


def propose_for_cluster(conn: sqlite3.Connection, cluster_id: int,
                         *, override_budget_usd: float | None = None) -> dict | None:
    rep = conn.execute(
        """SELECT pc.id, COALESCE(pc.auto_label, pc.label) AS label, t.user_text
           FROM prompt_clusters pc JOIN turns t ON t.id = pc.representative_turn_id
           WHERE pc.id = ?""", (cluster_id,),
    ).fetchone()
    if not rep:
        return None
    members = _members(conn, cluster_id, k=5)
    members_block = "\n---\n".join(members) if members else "(none)"

    client = AnthropicClient(conn, override_budget_usd=override_budget_usd)
    try:
        res = client.call(
            feature="diet",
            template=P.DIET,
            user_kwargs={
                "cluster_label": rep["label"] or f"cluster #{cluster_id}",
                "original": (rep["user_text"] or "").strip()[:1000],
                "members_block": members_block[:2000],
            },
            schema=S.DIET,
            target_id=cluster_id,
            max_tokens=600, timeout=30.0,
        )
    except BudgetExceeded:
        return None

    p = res.parsed
    _ensure_table(conn)
    with conn:
        cur = conn.execute(
            """INSERT INTO prompt_diet_suggestions(
                cluster_id, original_representative, tightened_text,
                predicted_savings_pct, reasoning, model, cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cluster_id, rep["user_text"], p["tightened_text"],
             float(p.get("predicted_savings_pct") or 0),
             p.get("reasoning"), res.model, res.cost_usd),
        )
        suggestion_id = cur.lastrowid
    return {"id": suggestion_id, "cluster_id": cluster_id,
            "tightened_text": p["tightened_text"],
            "predicted_savings_pct": p.get("predicted_savings_pct"),
            "reasoning": p.get("reasoning")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bin/diet")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--min-members", type=int, default=5)
    p.add_argument("--backfill-budget", type=float, default=None)
    p.add_argument("--cluster-id", type=int, default=None,
                   help="propose for one specific cluster")
    args = p.parse_args(argv)

    conn = connect()
    _ensure_table(conn)

    if args.cluster_id:
        cs = [{"id": args.cluster_id}]
    else:
        cs = _candidate_clusters(conn, min_members=args.min_members,
                                  limit=args.limit)
    if not cs:
        print("no candidates"); return 0

    print(f"proposing diet suggestions for {len(cs)} clusters…")
    ok = fail = 0
    for c in cs:
        try:
            r = propose_for_cluster(conn, c["id"],
                                     override_budget_usd=args.backfill_budget)
            if r:
                ok += 1
                print(f"  #{c['id']:3d} → savings {r['predicted_savings_pct']:.0f}% : "
                      f"{r['tightened_text'][:80]!r}")
            else:
                print(f"  #{c['id']:3d} skipped")
        except Exception as e:
            fail += 1
            print(f"  #{c['id']:3d} FAILED: {e!r}")
    print(f"done: {ok} ok, {fail} fail")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
