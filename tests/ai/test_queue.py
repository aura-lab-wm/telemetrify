"""Regression tests for two Smart Rerun Queue bugs:

1. score_candidates() must never surface a Claude Code image-paste
   placeholder line as `user_text_snippet`, and must never raise a bare
   IndexError on empty/whitespace-only user_text -- same class of bug fixed
   in cluster_label._members_for_cluster and diet._members.

2. A turn whose started_at can't be parsed must not be treated as
   "9999 days stale" (which would artificially boost it to the top of the
   queue via a saturated age_norm) -- it must be excluded from ranking.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from telemetrify.ai.queue import score_candidates

ASSISTANT_TEXT = "a" * 60  # must be > 50 chars to pass the candidate-set filter
BARE_MARKER = "[Image #2]"


def _make_session(conn: sqlite3.Connection, session_id: str = "sess-1") -> None:
    conn.execute(
        "INSERT INTO sessions(id, started_at, cwd) VALUES (?, datetime('now'), '/tmp')",
        (session_id,),
    )


def _make_turn(conn: sqlite3.Connection, user_text: str, started_at: str,
               *, session_id: str = "sess-1") -> int:
    cur = conn.execute(
        """
        INSERT INTO turns(session_id, user_text, assistant_text, started_at, origin)
        VALUES (?, ?, ?, ?, 'organic')
        """,
        (session_id, user_text, ASSISTANT_TEXT, started_at),
    )
    return cur.lastrowid


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_snippet_skips_image_placeholder_and_uses_next_real_line(migrated_db):
    _make_session(migrated_db)
    user_text = f"{BARE_MARKER}\nplease look at this failing test and fix it"
    _make_turn(migrated_db, user_text, _iso(5))

    candidates = score_candidates(migrated_db, k=20)

    assert len(candidates) == 1
    assert candidates[0]["user_text_snippet"] != BARE_MARKER
    assert "please look at this failing test" in candidates[0]["user_text_snippet"]


def test_snippet_handles_whitespace_only_user_text_without_raising(migrated_db):
    _make_session(migrated_db)
    # length > 12 so it passes the candidate-set filter, but strips to "".
    _make_turn(migrated_db, " " * 20, _iso(5))

    candidates = score_candidates(migrated_db, k=20)  # must not raise IndexError

    assert len(candidates) == 1
    assert candidates[0]["user_text_snippet"] == ""


def test_malformed_started_at_is_excluded_not_treated_as_maximally_stale(migrated_db):
    """Regression test: an unparseable started_at used to default to 9999
    "days stale" (saturating age_norm to 1.0, the top score for that
    component) instead of being excluded as bad data."""
    _make_session(migrated_db)
    good_turn = _make_turn(migrated_db, "a perfectly normal prompt here", _iso(5))
    _make_turn(migrated_db, "another normal prompt, just bad timestamp", "not-a-real-timestamp")

    candidates = score_candidates(migrated_db, k=20)

    assert [c["turn_id"] for c in candidates] == [good_turn]
