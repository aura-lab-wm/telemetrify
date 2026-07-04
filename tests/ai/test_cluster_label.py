"""_members_for_cluster must never surface Claude Code's image-paste
placeholder text (either the "[Image: original WxH...]" resize-hint form or
the bare "[Image #N]" form) as a representative prompt for the auto-labeling
LLM, and an all-screenshot cluster must get an honest fallback label instead
of that raw placeholder text verbatim.

Regression test for: cluster_label auto_label ending up as literally
"[Image: original 3024x80, displayed at 2000x53. Multiply coordinates by
1.51 to map to original image.]" on the "top clusters" / "cluster correction
breakdown" charts.
"""
from __future__ import annotations

import sqlite3

from telemetrify.ai.cluster_label import (
    FALLBACK_IMAGE_ONLY_LABEL,
    _cluster_has_only_image_placeholders,
    _is_image_placeholder,
    _members_for_cluster,
    label_cluster,
)

RESIZE_HINT = (
    "[Image: original 3024x80, displayed at 2000x53. Multiply coordinates "
    "by 1.51 to map to original image.]"
)
BARE_MARKER = "[Image #2]"


def _make_session(conn: sqlite3.Connection, session_id: str = "sess-1") -> None:
    conn.execute(
        "INSERT INTO sessions(id, started_at, cwd) VALUES (?, datetime('now'), '/tmp')",
        (session_id,),
    )


def _make_turn(conn: sqlite3.Connection, user_text: str, *, session_id: str = "sess-1") -> int:
    cur = conn.execute(
        """
        INSERT INTO turns(session_id, user_text, assistant_text, started_at)
        VALUES (?, ?, 'ok', datetime('now'))
        """,
        (session_id, user_text),
    )
    return cur.lastrowid


def _make_cluster(conn: sqlite3.Connection, turn_ids: list[int]) -> int:
    cur = conn.execute(
        "INSERT INTO prompt_clusters(member_count) VALUES (?)", (len(turn_ids),)
    )
    cluster_id = cur.lastrowid
    for i, tid in enumerate(turn_ids):
        conn.execute(
            "INSERT INTO turn_cluster(turn_id, cluster_id, similarity_to_centroid) VALUES (?, ?, ?)",
            (tid, cluster_id, 1.0 - i * 0.01),
        )
    conn.commit()
    return cluster_id


def test_is_image_placeholder_matches_both_forms():
    assert _is_image_placeholder(RESIZE_HINT)
    assert _is_image_placeholder(BARE_MARKER)
    assert not _is_image_placeholder("please fix this bug")
    assert not _is_image_placeholder("")


def test_members_for_cluster_excludes_pure_image_placeholder_line(migrated_db):
    _make_session(migrated_db)
    t1 = _make_turn(migrated_db, RESIZE_HINT)
    t2 = _make_turn(migrated_db, "fix the failing pytest in test_router.py")
    cid = _make_cluster(migrated_db, [t1, t2])

    members = _members_for_cluster(migrated_db, cid, k=5)

    assert RESIZE_HINT not in members
    assert BARE_MARKER not in members
    assert any("fix the failing pytest" in m for m in members)
    # the image-only turn contributed nothing -- only the real-text turn did
    assert len(members) == 1


def test_members_for_cluster_skips_placeholder_to_real_caption_below(migrated_db):
    """A turn whose user_text is the image marker followed by a real caption
    line should surface the caption, not the marker."""
    _make_session(migrated_db)
    user_text = f"{BARE_MARKER}\nactually the red box is the bug, please look there"
    t1 = _make_turn(migrated_db, user_text)
    cid = _make_cluster(migrated_db, [t1])

    members = _members_for_cluster(migrated_db, cid, k=5)

    assert len(members) == 1
    assert "red box" in members[0]


def test_all_image_placeholder_cluster_detected(migrated_db):
    _make_session(migrated_db)
    t1 = _make_turn(migrated_db, RESIZE_HINT)
    t2 = _make_turn(migrated_db, BARE_MARKER)
    cid = _make_cluster(migrated_db, [t1, t2])

    assert _members_for_cluster(migrated_db, cid, k=5) == []
    assert _cluster_has_only_image_placeholders(migrated_db, cid, k=5) is True


def test_empty_cluster_is_not_flagged_as_image_only(migrated_db):
    """A cluster with zero representative rows is a different case than an
    all-placeholder cluster -- it must not be treated as image-only."""
    cur = migrated_db.execute("INSERT INTO prompt_clusters(member_count) VALUES (0)")
    cid = cur.lastrowid
    migrated_db.commit()

    assert _members_for_cluster(migrated_db, cid, k=5) == []
    assert _cluster_has_only_image_placeholders(migrated_db, cid, k=5) is False


def test_label_cluster_all_image_only_gets_honest_fallback_not_raw_metadata(migrated_db):
    """The actual regression: an all-screenshot cluster must never end up
    with auto_label == the raw placeholder text."""
    _make_session(migrated_db)
    t1 = _make_turn(migrated_db, RESIZE_HINT)
    t2 = _make_turn(migrated_db, RESIZE_HINT.replace("3024x80", "5120x60"))
    cid = _make_cluster(migrated_db, [t1, t2])

    result = label_cluster(migrated_db, cid)

    assert result is not None
    assert result["label"] == FALLBACK_IMAGE_ONLY_LABEL
    assert "[image" not in result["label"].lower()

    row = migrated_db.execute(
        "SELECT auto_label FROM prompt_clusters WHERE id = ?", (cid,)
    ).fetchone()
    assert row["auto_label"] == FALLBACK_IMAGE_ONLY_LABEL
    assert "3024x80" not in row["auto_label"]
    assert "5120x60" not in row["auto_label"]
