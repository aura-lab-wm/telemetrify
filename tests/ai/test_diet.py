"""Regression tests for two Prompt Diet Analyzer bugs:

1. _candidate_clusters() must key "follow-up rate" off of turns that
   *received* a follow-up (turn_followups.prev_turn_id), not turns that
   *are themselves* a follow-up to something else (turn_followups.turn_id).
   Joining on the wrong column inverts the "target clusters with a high
   follow-up rate" signal the analyzer is supposed to implement.

2. _members() must never surface a Claude Code image-paste placeholder line
   verbatim, and must never raise on empty/whitespace-only user_text -- same
   class of bug already fixed in cluster_label._members_for_cluster.
"""
from __future__ import annotations

import sqlite3

from telemetrify.ai.diet import _candidate_clusters, _members


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
    for tid in turn_ids:
        conn.execute(
            "INSERT INTO turn_cluster(turn_id, cluster_id) VALUES (?, ?)",
            (tid, cluster_id),
        )
    conn.commit()
    return cluster_id


def _by_id(clusters: list[dict], cluster_id: int) -> dict:
    return next(c for c in clusters if c["id"] == cluster_id)


def test_followup_rate_counts_turns_that_were_corrected_not_the_correctors(migrated_db):
    """Regression test for the inverted-signal bug: a cluster containing the
    turn that got corrected should show a high followup_rate; a cluster
    containing only the *corrective* follow-up turn itself should show 0 --
    not the other way around."""
    _make_session(migrated_db)
    original = _make_turn(migrated_db, "please refactor this function")
    corrector = _make_turn(migrated_db, "no, actually keep the old signature")

    # `corrector` is the follow-up TO `original`.
    migrated_db.execute(
        "INSERT INTO turn_followups(turn_id, prev_turn_id, kind) VALUES (?, ?, 'corrective')",
        (corrector, original),
    )
    migrated_db.commit()

    cluster_corrected = _make_cluster(migrated_db, [original])
    cluster_corrector = _make_cluster(migrated_db, [corrector])

    clusters = _candidate_clusters(migrated_db, min_members=1, max_quality=4.0, limit=20)

    corrected_row = _by_id(clusters, cluster_corrected)
    corrector_row = _by_id(clusters, cluster_corrector)

    assert corrected_row["followup_rate"] == 1.0
    assert corrector_row["followup_rate"] == 0.0


def test_followup_rate_not_inflated_by_multiple_followup_rows_per_turn(migrated_db):
    """A turn with more than one turn_followups row pointing at it must still
    only count once toward followup_rate (no fan-out via the join)."""
    _make_session(migrated_db)
    original = _make_turn(migrated_db, "please refactor this function")
    corrector_a = _make_turn(migrated_db, "no, actually keep the old signature")
    corrector_b = _make_turn(migrated_db, "wait, undo that")

    migrated_db.execute(
        "INSERT INTO turn_followups(turn_id, prev_turn_id, kind) VALUES (?, ?, 'corrective')",
        (corrector_a, original),
    )
    migrated_db.execute(
        "INSERT INTO turn_followups(turn_id, prev_turn_id, kind) VALUES (?, ?, 'corrective')",
        (corrector_b, original),
    )
    migrated_db.commit()

    other = _make_turn(migrated_db, "an unrelated, uncorrected prompt")
    cluster_id = _make_cluster(migrated_db, [original, other])

    clusters = _candidate_clusters(migrated_db, min_members=1, max_quality=4.0, limit=20)
    row = _by_id(clusters, cluster_id)

    # 1 of 2 members corrected -- not 2/2 (or higher) from a fanned-out join.
    assert row["followup_rate"] == 0.5


def test_members_skips_image_placeholder_line(migrated_db):
    _make_session(migrated_db)
    placeholder = "[Image #2]"
    t1 = _make_turn(migrated_db, placeholder)
    t2 = _make_turn(migrated_db, "fix the failing test")
    cid = _make_cluster(migrated_db, [t1, t2])

    members = _members(migrated_db, cid, k=5)

    assert placeholder not in members
    assert any("fix the failing test" in m for m in members)


def test_members_handles_whitespace_only_user_text_without_raising(migrated_db):
    """Whitespace-only user_text used to make `.strip().splitlines()[0]` raise
    IndexError (splitlines() on an empty-after-strip string is [])."""
    _make_session(migrated_db)
    t1 = _make_turn(migrated_db, "   ")
    t2 = _make_turn(migrated_db, "a real prompt")
    cid = _make_cluster(migrated_db, [t1, t2])

    members = _members(migrated_db, cid, k=5)

    assert members == ["a real prompt"]
