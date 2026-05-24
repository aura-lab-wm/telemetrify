"""Smart Rerun Queue — score turns worth replaying, surface as a ranked list.

Score = 0.40 * cluster_size_norm
      + 0.30 * days_since_last_rerun_norm
      + 0.20 * (1 - quality / 5)        — only if auto-graded
      + 0.10 * has_followup

Candidates with a rerun in the last 30 days are excluded.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def score_candidates(conn: sqlite3.Connection, *, k: int = 20) -> list[dict]:
    """Return top-k candidate turns with their score components."""
    # Fetch a candidate set; we score in Python because SQL window-function
    # gymnastics for this would be ugly and the candidate count is small (<200).
    rows = conn.execute(
        """
        SELECT t.id AS turn_id, t.user_text, t.started_at, t.cwd, t.model,
               t.session_id, t.origin,
               g.quality AS quality,
               COALESCE(pc.auto_label, pc.label) AS cluster_label,
               pc.id AS cluster_id,
               pc.member_count AS cluster_members,
               EXISTS(SELECT 1 FROM turn_followups f WHERE f.prev_turn_id = t.id) AS has_followup,
               (SELECT MAX(r.run_at) FROM reruns r WHERE r.original_turn_id = t.id) AS last_rerun_at
        FROM turns t
        LEFT JOIN auto_grades g  ON g.turn_id = t.id
        LEFT JOIN turn_cluster tc ON tc.turn_id = t.id
        LEFT JOIN prompt_clusters pc ON pc.id = tc.cluster_id
        WHERE t.origin = 'organic'
          AND length(t.user_text) > 12
          AND length(t.assistant_text) > 50
        """
    ).fetchall()

    candidates = []
    now = datetime.now(timezone.utc)
    # Pre-compute normalisers over the candidate set.
    max_cluster = max((int(r["cluster_members"] or 0) for r in rows), default=1) or 1

    for r in rows:
        # Skip if recently rerun (< 30 days).
        last = r["last_rerun_at"]
        days_since_rerun = 9999.0
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                days_since_rerun = (now - last_dt).total_seconds() / 86400.0
                if days_since_rerun < 30:
                    continue
            except Exception:
                pass

        # Age in days from started_at.
        days_old = 9999.0
        try:
            dt = datetime.fromisoformat((r["started_at"] or "").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_old = max(0.0, (now - dt).total_seconds() / 86400.0)
        except Exception:
            pass

        cluster_n = int(r["cluster_members"] or 0)
        cluster_norm = (cluster_n / max_cluster) if max_cluster else 0.0
        # Age norm: 0d→0, 60d→1 (saturate).
        age_norm = min(1.0, days_old / 60.0)
        quality = r["quality"]
        if quality is None:
            quality_term = 0.4   # mid-prior for ungraded
        else:
            quality_term = max(0.0, min(1.0, 1.0 - (int(quality) / 5.0)))
        followup_term = 1.0 if int(r["has_followup"] or 0) else 0.0

        score = (
            0.40 * cluster_norm
            + 0.30 * age_norm
            + 0.20 * quality_term
            + 0.10 * followup_term
        )
        candidates.append({
            "turn_id": int(r["turn_id"]),
            "user_text_snippet": (r["user_text"] or "").strip().splitlines()[0][:160],
            "cwd": r["cwd"],
            "model": r["model"],
            "session_id": r["session_id"],
            "cluster_id": r["cluster_id"],
            "cluster_label": r["cluster_label"],
            "cluster_members": cluster_n,
            "has_followup": bool(r["has_followup"]),
            "quality": int(quality) if quality is not None else None,
            "days_old": round(days_old, 1),
            "days_since_rerun": round(days_since_rerun, 1) if last else None,
            "score": round(score, 4),
            "components": {
                "cluster_norm":  round(cluster_norm, 3),
                "age_norm":      round(age_norm, 3),
                "quality_term":  round(quality_term, 3),
                "followup_term": followup_term,
            },
            # Cost estimate for a rerun at the same model as the original turn
            # (rough — $0.50 ceiling as per the rerun.run_rerun default budget).
            "estimated_cost_usd": 0.50,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:k]
