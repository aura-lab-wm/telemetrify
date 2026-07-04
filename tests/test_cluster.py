"""Regression tests for telemetrify.cluster.rebuild_clusters.

BUG 1 (critical): rebuild_clusters used to unconditionally
`DELETE FROM turn_cluster` before reinserting rows only for the turns it
actually reprocessed this run (the WINDOW_RECENT-most-recent + annotated
turns) -- silently discarding cluster membership for every turn outside that
window on every rebuild. WINDOW_RECENT is a deliberate perf cap (see the
module docstring: "so per-rebuild cost stays bounded"), not a signal that
out-of-window turns should be excluded from clustering results. The fix
scopes the turn_cluster delete to the turns actually reprocessed, and also
preserves any prompt_clusters row still referenced by an out-of-window
turn_cluster row (prompt_clusters has ON DELETE SET NULL on
turn_cluster.cluster_id, so deleting such a row would cascade into wiping
those turns' membership too -- the same bug, one hop removed).

BUG 2/3: the representative-turn label line used to
(a) crash with an IndexError when the representative's user_text is
    empty/whitespace-only (`"".splitlines()` is `[]`, so `[0]` raises), and
(b) surface Claude Code's image-paste placeholder text (the resize-hint
    "[Image: original WxH...]" form, or the bare "[Image #N]" form) as the
    human-facing cluster label verbatim when that placeholder was the first
    line of the representative turn's user_text.
Fixed via _label_from_user_text, which reuses the exact
_is_image_placeholder helper ai/cluster_label.py already applies to this
same problem rather than re-implementing the regex.
"""
from __future__ import annotations

import numpy as np
import pytest

from telemetrify import cluster
from telemetrify.cluster import _label_from_user_text
from telemetrify.db import EMBEDDING_DIM
from telemetrify.store import insert_turn, upsert_session
from telemetrify.transcript import Turn

RESIZE_HINT = (
    "[Image: original 3024x80, displayed at 2000x53. Multiply coordinates "
    "by 1.51 to map to original image.]"
)


def _unit(v) -> list[float]:
    v = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


def _mk_turn(uuid: str, text: str, started_at: str) -> Turn:
    return Turn(
        session_id="s1", user_uuid=uuid, parent_uuid=None, prompt_id=None,
        user_text=text, assistant_text="ok", thinking_text="",
        model="claude-opus-4-7", started_at=started_at, finished_at=started_at,
        latency_ms=10, input_tokens=1, output_tokens=1,
        cache_creation_tokens=0, cache_read_tokens=0,
        cwd="/x", git_branch=None, project_dir=None, transcript_path=None,
        cli_version=None, entrypoint=None, user_type=None,
        attribution_skill=None, attribution_plugin=None,
        tool_call_count=0, assistant_message_count=1, tool_calls=[], raw_json="",
    )


def _add(conn, uuid: str, text: str, started_at: str, emb: list[float]) -> int:
    t = _mk_turn(uuid, text, started_at)
    upsert_session(conn, t)
    return insert_turn(conn, t, None, prompt_embedding=emb)


def _two_cluster_embeddings():
    """Two groups of 4 unit vectors, each forming its own well-separated,
    HDBSCAN-stable cluster.

    A single homogeneous blob with nothing to contrast against gets labeled
    all-noise by HDBSCAN's stability selection (verified empirically) -- two
    real, separated clusters is the minimal reliable setup to get non-noise
    labels out of fit_predict with the default min_cluster_size=3.

    Within each group, member 0 sits exactly at the group's mean direction;
    members 1-3 are +/- perturbations at 120 degrees from each other so they
    cancel in the mean. That makes member 0 deterministically the closest
    point to the group's centroid -- i.e. the representative turn
    rebuild_clusters will pick for that group -- regardless of what order
    SQLite happens to return rows in.
    """
    ea = np.zeros(EMBEDDING_DIM); ea[6] = 1.0
    eb = np.zeros(EMBEDDING_DIM); eb[7] = 1.0
    eps = 0.05
    perturb = [
        np.cos(np.deg2rad(k * 120)) * ea + np.sin(np.deg2rad(k * 120)) * eb
        for k in range(3)
    ]

    v0 = np.zeros(EMBEDDING_DIM); v0[5] = 1.0
    group_a = [_unit(v0)] + [_unit(v0 + eps * p) for p in perturb]

    v1 = np.zeros(EMBEDDING_DIM); v1[300] = 1.0
    group_b = [_unit(v1)] + [_unit(v1 + eps * p) for p in perturb]

    return group_a, group_b


# ─── BUG 1: rebuild must not blanket-wipe out-of-window membership ─────────

def test_rebuild_preserves_cluster_membership_for_turns_outside_window(migrated_db, monkeypatch):
    conn = migrated_db
    group_a, group_b = _two_cluster_embeddings()

    # An "old" turn that will fall OUTSIDE this run's reprocessing window
    # (oldest started_at, not annotated) and already has a cluster
    # assignment from some earlier rebuild -- simulating real steady-state
    # DB content that a rebuild must not disturb.
    old_emb = [0.0] * EMBEDDING_DIM
    old_emb[200] = 1.0
    old_turn_id = _add(conn, "old-1", "old leftover turn", "2020-01-01T00:00:00.000Z", old_emb)

    cur = conn.execute(
        "INSERT INTO prompt_clusters(label, representative_turn_id, member_count) VALUES (?, ?, ?)",
        ("old topic", old_turn_id, 1),
    )
    old_cluster_id = cur.lastrowid
    conn.execute(
        "INSERT INTO turn_cluster(turn_id, cluster_id, similarity_to_centroid) VALUES (?, ?, ?)",
        (old_turn_id, old_cluster_id, 1.0),
    )
    conn.commit()

    # 8 "new" turns forming two real HDBSCAN clusters -- these ARE inside
    # the (monkeypatched, small) reprocessing window.
    new_ids = []
    for i, emb in enumerate(group_a + group_b):
        tid = _add(conn, f"new-{i}", f"new prompt {i}",
                    f"2026-06-03T00:00:{i:02d}.000Z", emb)
        new_ids.append(tid)

    monkeypatch.setattr(cluster, "WINDOW_RECENT", len(new_ids))

    summary = cluster.rebuild_clusters(conn, min_cluster_size=3, log=lambda *_a: None)
    assert summary["clusters"] >= 1, f"expected at least one real cluster, got {summary!r}"

    # The old, out-of-window cluster row must NOT have been deleted...
    old_cluster_row = conn.execute(
        "SELECT id FROM prompt_clusters WHERE id=?", (old_cluster_id,)
    ).fetchone()
    assert old_cluster_row is not None, (
        "rebuild_clusters deleted a prompt_clusters row still referenced by an "
        "out-of-window turn -- this is bug 1 (blanket wipe / cascading delete "
        "via prompt_clusters' ON DELETE SET NULL)"
    )

    # ...and the old turn's own membership row must be intact and unchanged.
    tc = conn.execute(
        "SELECT cluster_id FROM turn_cluster WHERE turn_id=?", (old_turn_id,)
    ).fetchone()
    assert tc is not None, (
        "rebuild_clusters wiped turn_cluster membership for a turn outside "
        "the reprocessing window -- this is bug 1"
    )
    assert tc["cluster_id"] == old_cluster_id

    # Sanity: the new, in-window turns actually got clustered too.
    for tid in new_ids:
        row = conn.execute(
            "SELECT cluster_id FROM turn_cluster WHERE turn_id=?", (tid,)
        ).fetchone()
        assert row is not None and row["cluster_id"] is not None

    # A second rebuild (idempotent, per the docstring) must not cumulatively
    # lose the out-of-window membership either.
    cluster.rebuild_clusters(conn, min_cluster_size=3, log=lambda *_a: None)
    old_cluster_row2 = conn.execute(
        "SELECT id FROM prompt_clusters WHERE id=?", (old_cluster_id,)
    ).fetchone()
    tc2 = conn.execute(
        "SELECT cluster_id FROM turn_cluster WHERE turn_id=?", (old_turn_id,)
    ).fetchone()
    assert old_cluster_row2 is not None
    assert tc2 is not None and tc2["cluster_id"] == old_cluster_id


# ─── BUG 2/3: representative-turn label must not crash or leak placeholders ─

def test_label_from_user_text_empty_or_whitespace_does_not_crash():
    """Bug 3: `"".splitlines()[0]` raises IndexError. Must guard, not crash."""
    assert _label_from_user_text("") == ""
    assert _label_from_user_text("   \n   \n") == ""
    assert _label_from_user_text(None) == ""


def test_label_from_user_text_skips_image_placeholder_lines():
    """Bug 2: an image-paste placeholder must never become the label verbatim;
    a real caption on another line should still surface."""
    assert _label_from_user_text(RESIZE_HINT) == ""
    assert _label_from_user_text("[Image #2]") == ""
    text = f"{RESIZE_HINT}\nactually please fix the bug in router.py"
    assert _label_from_user_text(text) == "actually please fix the bug in router.py"


def test_label_from_user_text_normal_text_truncated_to_120():
    assert _label_from_user_text("fix the bug\nmore context") == "fix the bug"
    long_line = "x" * 200
    assert _label_from_user_text(long_line) == long_line[:120]


def test_rebuild_cluster_label_skips_image_placeholder_for_representative_turn(migrated_db):
    """End-to-end: when the turn HDBSCAN picks as a cluster's representative
    has image-placeholder-only user_text, the persisted `label` must not be
    that raw placeholder text (and the rebuild must not crash)."""
    conn = migrated_db
    group_a, group_b = _two_cluster_embeddings()

    texts_a = [RESIZE_HINT, "unrelated a1", "unrelated a2", "unrelated a3"]
    for i, (emb, text) in enumerate(zip(group_a, texts_a)):
        _add(conn, f"a-{i}", text, f"2026-06-03T00:00:{i:02d}.000Z", emb)
    texts_b = ["unrelated b0", "unrelated b1", "unrelated b2", "unrelated b3"]
    for i, (emb, text) in enumerate(zip(group_b, texts_b)):
        _add(conn, f"b-{i}", text, f"2026-06-03T00:01:{i:02d}.000Z", emb)

    summary = cluster.rebuild_clusters(conn, min_cluster_size=3, log=lambda *_a: None)
    assert summary["clusters"] >= 1

    rows = conn.execute("SELECT label FROM prompt_clusters").fetchall()
    labels = [r["label"] for r in rows]
    assert not any("[image" in (l or "").lower() for l in labels), f"labels={labels!r}"


def test_rebuild_cluster_representative_with_empty_user_text_does_not_crash(migrated_db):
    """End-to-end: an empty/whitespace-only representative turn must not
    crash the whole rebuild (bug 3)."""
    conn = migrated_db
    group_a, group_b = _two_cluster_embeddings()

    texts_a = ["   ", "unrelated a1", "unrelated a2", "unrelated a3"]
    for i, (emb, text) in enumerate(zip(group_a, texts_a)):
        _add(conn, f"a-{i}", text, f"2026-06-03T00:00:{i:02d}.000Z", emb)
    texts_b = ["unrelated b0", "unrelated b1", "unrelated b2", "unrelated b3"]
    for i, (emb, text) in enumerate(zip(group_b, texts_b)):
        _add(conn, f"b-{i}", text, f"2026-06-03T00:01:{i:02d}.000Z", emb)

    # must not raise IndexError
    summary = cluster.rebuild_clusters(conn, min_cluster_size=3, log=lambda *_a: None)
    assert summary["clusters"] >= 1
