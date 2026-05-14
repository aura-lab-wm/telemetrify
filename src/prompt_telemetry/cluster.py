"""Prompt clustering via HDBSCAN.

Two modes:
- `rebuild_clusters()`: run HDBSCAN over a window of prompt embeddings, recreate
  `prompt_clusters` and `turn_cluster` rows. Idempotent.
- `assign_nearest_cluster()`: for a new turn at capture time, find the best
  existing cluster by centroid distance and persist the assignment (if close enough).

The clustering window is the 2000 most-recent + all annotated turns, by default,
so per-rebuild cost stays bounded.
"""
from __future__ import annotations

import sqlite3
import struct
from collections import defaultdict
from typing import Iterable

import numpy as np

from .db import serialize_embedding


WINDOW_RECENT = 2000
NEAREST_CLUSTER_MAX_DIST = 0.30


def _unpack(b: bytes) -> np.ndarray:
    n = len(b) // 4
    return np.array(struct.unpack(f"{n}f", b), dtype=np.float32)


def _load_window(conn: sqlite3.Connection) -> tuple[list[int], np.ndarray]:
    rows = conn.execute(
        """
        SELECT t.id, pv.embedding
        FROM turns t
        JOIN prompt_vec pv ON pv.turn_id = t.id
        WHERE t.id IN (
            SELECT id FROM turns ORDER BY started_at DESC LIMIT ?
        ) OR t.id IN (SELECT turn_id FROM annotations)
        """,
        (WINDOW_RECENT,),
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 384), dtype=np.float32)
    ids = [r["id"] for r in rows]
    mat = np.vstack([_unpack(r["embedding"]) for r in rows])
    return ids, mat


def rebuild_clusters(conn: sqlite3.Connection, min_cluster_size: int = 3, log=print) -> dict:
    """Run HDBSCAN over the window. Persist clusters + assignments.
    Returns a small summary dict.
    """
    import hdbscan

    ids, mat = _load_window(conn)
    if mat.shape[0] < min_cluster_size:
        log(f"only {mat.shape[0]} prompts in window — skipping cluster rebuild")
        return {"prompts": int(mat.shape[0]), "clusters": 0, "noise": int(mat.shape[0])}

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        metric="euclidean",  # since vectors are normalized, euclidean ≈ cosine
        cluster_selection_method="eom",
    )
    labels = clusterer.fit_predict(mat)

    # Group turns by raw HDBSCAN label.
    by_label: dict[int, list[int]] = defaultdict(list)
    for tid, lbl in zip(ids, labels):
        by_label[int(lbl)].append(int(tid))

    # Compute centroids and pick a representative (closest to centroid).
    new_clusters: list[tuple[int, list[int], np.ndarray]] = []  # (label, turn_ids, centroid)
    id_to_row = {tid: i for i, tid in enumerate(ids)}
    for lbl, tids in by_label.items():
        if lbl == -1:
            continue  # HDBSCAN noise
        rows = mat[[id_to_row[t] for t in tids]]
        centroid = rows.mean(axis=0)
        centroid /= max(np.linalg.norm(centroid), 1e-9)
        new_clusters.append((lbl, tids, centroid))

    # Stable mapping: try to keep existing cluster_ids by matching old centroids to new ones.
    old = conn.execute(
        """
        SELECT pc.id, pc.representative_turn_id, pv.embedding
        FROM prompt_clusters pc
        LEFT JOIN prompt_vec pv ON pv.turn_id = pc.representative_turn_id
        """
    ).fetchall()
    old_centroids = []
    old_ids = []
    for r in old:
        if r["embedding"]:
            old_centroids.append(_unpack(r["embedding"]))
            old_ids.append(r["id"])

    # Map new-cluster index → reused cluster_id (or None for new).
    reused: dict[int, int] = {}
    if old_centroids:
        OC = np.vstack(old_centroids)
        for ni, (_lbl, _tids, c) in enumerate(new_clusters):
            sims = OC @ c
            best = int(np.argmax(sims))
            if sims[best] >= 0.85 and old_ids[best] not in reused.values():
                reused[ni] = old_ids[best]

    # Wipe and rewrite assignments + cluster metadata.
    with conn:
        conn.execute("DELETE FROM turn_cluster")
        # Don't delete prompt_clusters wholesale; reuse rows where possible.
        kept_ids = set(reused.values())
        conn.execute(f"DELETE FROM prompt_clusters WHERE id NOT IN ({','.join(['?']*len(kept_ids)) or 'NULL'})",
                     list(kept_ids) or [])

        for ni, (_lbl, tids, c) in enumerate(new_clusters):
            # Pick representative: turn whose embedding is closest to centroid.
            rows = mat[[id_to_row[t] for t in tids]]
            sims = rows @ c
            rep_idx = int(np.argmax(sims))
            rep_turn = tids[rep_idx]
            rep_text = conn.execute("SELECT user_text FROM turns WHERE id=?", (rep_turn,)).fetchone()
            label_text = (rep_text["user_text"] if rep_text else "").strip().splitlines()[0][:120] if rep_text else ""

            if ni in reused:
                cid = reused[ni]
                conn.execute(
                    "UPDATE prompt_clusters SET label=?, representative_turn_id=?, member_count=?, updated_at=datetime('now') WHERE id=?",
                    (label_text, rep_turn, len(tids), cid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO prompt_clusters(label, representative_turn_id, member_count) VALUES (?, ?, ?)",
                    (label_text, rep_turn, len(tids)),
                )
                cid = cur.lastrowid

            for tid in tids:
                sim = float(mat[id_to_row[tid]] @ c)
                conn.execute(
                    "INSERT OR REPLACE INTO turn_cluster(turn_id, cluster_id, similarity_to_centroid) VALUES (?, ?, ?)",
                    (tid, cid, sim),
                )

    log(f"rebuilt: {len(new_clusters)} clusters over {mat.shape[0]} prompts "
        f"(noise={sum(1 for l in labels if l == -1)})")
    return {
        "prompts": int(mat.shape[0]),
        "clusters": len(new_clusters),
        "noise": int(sum(1 for l in labels if l == -1)),
    }


def assign_nearest_cluster(conn: sqlite3.Connection, turn_id: int, prompt_emb: list[float]) -> int | None:
    """At capture time, find the nearest existing cluster centroid for this turn's prompt.
    Persist the assignment if within NEAREST_CLUSTER_MAX_DIST. Return cluster_id or None."""
    rows = conn.execute(
        """
        SELECT pc.id, pv.embedding
        FROM prompt_clusters pc
        JOIN prompt_vec pv ON pv.turn_id = pc.representative_turn_id
        """
    ).fetchall()
    if not rows:
        return None
    pe = np.array(prompt_emb, dtype=np.float32)
    pe /= max(np.linalg.norm(pe), 1e-9)
    best_id = None
    best_sim = -1.0
    for r in rows:
        c = _unpack(r["embedding"])
        c /= max(np.linalg.norm(c), 1e-9)
        s = float(pe @ c)
        if s > best_sim:
            best_sim = s
            best_id = r["id"]
    # similarity in [-1, 1]; distance = 1 - s
    distance = 1.0 - best_sim
    if distance > NEAREST_CLUSTER_MAX_DIST or best_id is None:
        return None
    conn.execute(
        "INSERT OR REPLACE INTO turn_cluster(turn_id, cluster_id, similarity_to_centroid) VALUES (?, ?, ?)",
        (turn_id, best_id, best_sim),
    )
    return best_id


def main(argv: list[str] | None = None) -> int:
    import sys
    from .db import connect
    cmd = (argv or sys.argv[1:])
    conn = connect()
    if not cmd or cmd[0] == "rebuild":
        summary = rebuild_clusters(conn)
        print(summary)
        return 0
    print("usage: python -m prompt_telemetry.cluster rebuild")
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
