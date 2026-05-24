"""Auto-label prompt clusters: read 5 representative member prompts per cluster
and ask an LLM to write a 2-7 word semantic caption.

CLI:
    python -m telemetrify.ai.cluster_label [--only-new] [--all] [--limit N]
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


def _members_for_cluster(conn: sqlite3.Connection, cluster_id: int, k: int = 5) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.user_text
        FROM turn_cluster tc JOIN turns t ON t.id = tc.turn_id
        WHERE tc.cluster_id = ?
        ORDER BY COALESCE(tc.similarity_to_centroid, 0) DESC
        LIMIT ?
        """,
        (cluster_id, k),
    ).fetchall()
    out = []
    for r in rows:
        s = (r["user_text"] or "").strip().splitlines()[0][:280]
        if s:
            out.append(s)
    return out


def label_cluster(conn: sqlite3.Connection, cluster_id: int, *,
                  override_budget_usd: float | None = None) -> dict | None:
    members = _members_for_cluster(conn, cluster_id, k=5)
    if len(members) < 1:
        return None
    # pad to 5 entries for the template
    while len(members) < 5:
        members.append("(no additional example)")
    kwargs = {f"example_{i+1}": m for i, m in enumerate(members[:5])}

    client = AnthropicClient(conn, override_budget_usd=override_budget_usd)
    try:
        res = client.call(
            feature="cluster_label",
            template=P.CLUSTER_LABEL,
            user_kwargs=kwargs,
            schema=S.CLUSTER_LABEL,
            target_id=cluster_id,
            max_tokens=80,
            timeout=20.0,
        )
    except BudgetExceeded:
        return None

    label = (res.parsed.get("label") or "").strip().lower()
    if not label:
        return None

    with conn:
        conn.execute(
            """
            UPDATE prompt_clusters
            SET auto_label = ?, auto_label_at = ?, auto_label_model = ?, auto_label_version = ?
            WHERE id = ?
            """,
            (label, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             res.model, res.prompt_version, cluster_id),
        )
    return {"cluster_id": cluster_id, "label": label,
            "model": res.model, "cost_usd": res.cost_usd}


def label_batch(*, only_new: bool = True, limit: int | None = None,
                override_budget_usd: float | None = None, log=print) -> dict:
    conn = connect()
    where = "auto_label IS NULL OR auto_label = ''" if only_new else "1=1"
    sql = f"SELECT id FROM prompt_clusters WHERE {where} ORDER BY member_count DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cluster_ids = [r["id"] for r in conn.execute(sql).fetchall()]
    if not cluster_ids:
        log("no clusters to label.")
        return {"labeled": 0, "skipped": 0, "failed": 0}

    log(f"labeling {len(cluster_ids)} clusters…")
    labeled = skipped = failed = 0
    for i, cid in enumerate(cluster_ids, 1):
        try:
            r = label_cluster(conn, cid, override_budget_usd=override_budget_usd)
            if r is None:
                skipped += 1
            else:
                labeled += 1
                log(f"  [{i:3d}/{len(cluster_ids)}] cluster #{cid:3d} → {r['label']!r}")
        except Exception as e:
            failed += 1
            log(f"  [{i:3d}/{len(cluster_ids)}] cluster #{cid:3d} FAILED: {e!r}")

    summary = {"labeled": labeled, "skipped": skipped, "failed": failed,
               "requested": len(cluster_ids)}
    log(f"done: {summary}")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bin/label-clusters")
    p.add_argument("--only-new", action="store_true", default=True,
                   help="only label clusters whose auto_label is empty (default)")
    p.add_argument("--all", action="store_true",
                   help="re-label every cluster (overrides --only-new)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--backfill-budget", type=float, default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    only_new = not args.all
    summary = label_batch(only_new=only_new, limit=args.limit,
                          override_budget_usd=args.backfill_budget)
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
