"""Regression tests for two Daily Digest bugs:

1. _stats()'s correction_pct must not be inflated by a fan-out LEFT JOIN
   against turn_followups when a turn has more than one follow-up row.

2. generate()'s default "day" must be the UTC calendar date (matching how
   turns.started_at is stored and how `date(t.started_at) = ?` filters run),
   not the local wall-clock date -- otherwise turns near local midnight land
   in the wrong day's digest.
"""
from __future__ import annotations

import datetime as dt_mod
import sqlite3

from telemetrify.ai import digest as digest_mod
from telemetrify.ai.digest import _stats


def _make_session(conn: sqlite3.Connection, session_id: str = "sess-1") -> None:
    conn.execute(
        "INSERT INTO sessions(id, started_at, cwd) VALUES (?, datetime('now'), '/tmp')",
        (session_id,),
    )


def _make_turn(conn: sqlite3.Connection, user_text: str, started_at: str,
               *, session_id: str = "sess-1") -> int:
    cur = conn.execute(
        """
        INSERT INTO turns(session_id, user_text, assistant_text, started_at)
        VALUES (?, ?, 'ok', ?)
        """,
        (session_id, user_text, started_at),
    )
    return cur.lastrowid


def test_correction_pct_not_inflated_by_multiple_followup_rows(migrated_db):
    _make_session(migrated_db)
    day = "2026-01-15"
    corrected = _make_turn(migrated_db, "please refactor this", f"{day} 10:00:00")
    clean = _make_turn(migrated_db, "an uncorrected prompt", f"{day} 11:00:00")
    # Two separate corrective turns both point back at the same original turn
    # -- this is exactly the fan-out shape that inflated the old query.
    corrector_a = _make_turn(migrated_db, "no, actually revert that", f"{day} 10:05:00")
    corrector_b = _make_turn(migrated_db, "wait, undo that too", f"{day} 10:10:00")
    migrated_db.execute(
        "INSERT INTO turn_followups(turn_id, prev_turn_id, kind) VALUES (?, ?, 'corrective')",
        (corrector_a, corrected),
    )
    migrated_db.execute(
        "INSERT INTO turn_followups(turn_id, prev_turn_id, kind) VALUES (?, ?, 'corrective')",
        (corrector_b, corrected),
    )
    migrated_db.commit()

    stats = _stats(migrated_db, day)

    # 4 turns total on this day (corrected, clean, corrector_a, corrector_b);
    # only `corrected` was itself followed up on -- 1/4 = 25%, not the
    # fanned-out 3/4 (or worse) the old LEFT JOIN produced.
    assert stats["turns_today"] == 4
    assert stats["correction_pct"] == 25.0


class _FakeResult:
    def __init__(self):
        self.parsed = {"summary": "ok"}
        self.model = "test-model"
        self.cost_usd = 0.0


class _FakeClient:
    def __init__(self, conn, override_budget_usd=None):
        pass

    def call(self, **kwargs):
        return _FakeResult()


def test_generate_default_day_uses_utc_calendar_date_not_local(migrated_db, monkeypatch):
    class _FixedDateTime(dt_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return cls(2026, 1, 16, 1, 0, 0, tzinfo=tz)  # 01:00 UTC on the 16th
            # A "local" reading that is still the 15th -- simulates an
            # operator west of UTC where local wall-clock lags the UTC day.
            return cls(2026, 1, 15, 20, 0, 0)

    monkeypatch.setattr(digest_mod, "datetime", _FixedDateTime)
    monkeypatch.setattr(digest_mod, "AnthropicClient", _FakeClient)

    _make_session(migrated_db)
    # Stamped just after UTC midnight on the 16th.
    _make_turn(migrated_db, "hello", "2026-01-16 00:30:00")

    result = digest_mod.generate(migrated_db, day=None)

    assert result is not None
    assert result["date"] == "2026-01-16"
