"""Auto-label prompt clusters: read 5 representative member prompts per cluster
and ask an LLM to write a 2-7 word semantic caption.

CLI:
    python -m telemetrify.ai.cluster_label [--only-new] [--all] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone

from . import prompts as P, schemas as S
from .client import AnthropicClient, BudgetExceeded
from ..db import connect

# Claude Code's paste convention for an image with no accompanying caption
# leaves user_text as one of these placeholder forms, e.g.:
#   "[Image: original 3024x80, displayed at 2000x53. Multiply coordinates by
#    1.51 to map to original image.]"
#   "[Image #2]"
# Neither is real user-authored text, so it must never be fed to the labeler
# nor allowed to become an auto_label verbatim.
_IMAGE_PLACEHOLDER_RE = re.compile(
    r"^\[Image(?::\s*original\s+\d+x\d+.*?|\s*#\d+)\]$",
    re.IGNORECASE,
)

FALLBACK_IMAGE_ONLY_LABEL = "(image-only prompts)"


def _is_image_placeholder(line: str) -> bool:
    return bool(_IMAGE_PLACEHOLDER_RE.match(line.strip()))


def _representative_rows(conn: sqlite3.Connection, cluster_id: int, k: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.user_text
        FROM turn_cluster tc JOIN turns t ON t.id = tc.turn_id
        WHERE tc.cluster_id = ?
        ORDER BY COALESCE(tc.similarity_to_centroid, 0) DESC
        LIMIT ?
        """,
        (cluster_id, k),
    ).fetchall()


def _members_for_cluster(conn: sqlite3.Connection, cluster_id: int, k: int = 5) -> list[str]:
    rows = _representative_rows(conn, cluster_id, k)
    out = []
    for r in rows:
        # Walk the turn's lines looking for the first one that isn't just an
        # image-paste placeholder (resize-hint or bare "[Image #N]" form).
        # This lets a real caption below/above an image marker still surface.
        for line in (r["user_text"] or "").strip().splitlines():
            line = line.strip()
            if not line or _is_image_placeholder(line):
                continue
            out.append(line[:280])
            break
    return out


def _cluster_has_only_image_placeholders(conn: sqlite3.Connection, cluster_id: int, k: int = 5) -> bool:
    """True when the cluster has representative members, but every one of
    them is nothing but image-paste placeholder text -- a real all-screenshot
    cluster with no usable caption anywhere. Distinguishes that case from a
    cluster with no representative rows at all (which _members_for_cluster
    also reports as empty, but which should stay unlabeled, not fall back)."""
    rows = _representative_rows(conn, cluster_id, k)
    if not rows:
        return False
    for r in rows:
        for line in (r["user_text"] or "").strip().splitlines():
            line = line.strip()
            if line and not _is_image_placeholder(line):
                return False
    return True


def label_cluster(conn: sqlite3.Connection, cluster_id: int, *,
                  override_budget_usd: float | None = None) -> dict | None:
    members = _members_for_cluster(conn, cluster_id, k=5)
    if len(members) < 1:
        if _cluster_has_only_image_placeholders(conn, cluster_id, k=5):
            # A real all-screenshot cluster: don't call the LLM with nothing
            # but placeholder text, and don't leave raw metadata behind --
            # stamp an honest, clearly-labeled fallback instead.
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with conn:
                conn.execute(
                    """
                    UPDATE prompt_clusters
                    SET auto_label = ?, auto_label_at = ?, auto_label_model = ?, auto_label_version = ?
                    WHERE id = ?
                    """,
                    (FALLBACK_IMAGE_ONLY_LABEL, now, "rule:image-only-fallback",
                     "image-only-fallback-v1", cluster_id),
                )
            return {"cluster_id": cluster_id, "label": FALLBACK_IMAGE_ONLY_LABEL,
                    "model": "rule:image-only-fallback", "cost_usd": 0.0}
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
