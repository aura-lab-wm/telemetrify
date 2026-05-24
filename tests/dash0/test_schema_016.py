"""Migration 016 — dash0 OTel ingest schema."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "prompt_telemetry" / "migrations"
)


def _apply_016(conn: sqlite3.Connection) -> None:
    sql = (MIGRATIONS_DIR / "016_dash0_otel.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


def test_016_creates_all_tables(blank_db: sqlite3.Connection):
    _apply_016(blank_db)
    tables = {
        row[0]
        for row in blank_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'dash0_%'"
        ).fetchall()
    }
    assert tables == {
        "dash0_resources",
        "dash0_spans",
        "dash0_span_events",
        "dash0_log_records",
    }


def test_016_creates_expected_indexes(blank_db: sqlite3.Connection):
    _apply_016(blank_db)
    idxs = {
        row[0]
        for row in blank_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_dash0_%'"
        ).fetchall()
    }
    for needed in (
        "idx_dash0_spans_conversation",
        "idx_dash0_spans_trace",
        "idx_dash0_spans_name",
        "idx_dash0_spans_received",
        "idx_dash0_span_events_span",
        "idx_dash0_log_records_conversation",
        "idx_dash0_log_records_span",
        "idx_dash0_log_records_received",
    ):
        assert needed in idxs, f"missing index {needed}"


def test_016_is_idempotent(blank_db: sqlite3.Connection):
    _apply_016(blank_db)
    _apply_016(blank_db)
    tables = {
        row[0]
        for row in blank_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'dash0_%'"
        ).fetchall()
    }
    assert len(tables) == 4


def test_016_resources_unique_fingerprint(blank_db: sqlite3.Connection):
    _apply_016(blank_db)
    blank_db.execute(
        "INSERT INTO dash0_resources(fingerprint, attrs_json) VALUES ('abc', '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        blank_db.execute(
            "INSERT INTO dash0_resources(fingerprint, attrs_json) VALUES ('abc', '{}')"
        )


def test_016_spans_pk_is_composite(blank_db: sqlite3.Connection):
    _apply_016(blank_db)
    blank_db.execute(
        "INSERT INTO dash0_spans(trace_id, span_id, name, start_ns) "
        "VALUES ('t1', 's1', 'foo', 1)"
    )
    # Same span_id under a different trace is allowed.
    blank_db.execute(
        "INSERT INTO dash0_spans(trace_id, span_id, name, start_ns) "
        "VALUES ('t2', 's1', 'foo', 2)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        blank_db.execute(
            "INSERT INTO dash0_spans(trace_id, span_id, name, start_ns) "
            "VALUES ('t1', 's1', 'foo', 3)"
        )


def test_016_spans_received_at_defaults(blank_db: sqlite3.Connection):
    _apply_016(blank_db)
    blank_db.execute(
        "INSERT INTO dash0_spans(trace_id, span_id, name, start_ns) "
        "VALUES ('t1', 's1', 'foo', 1)"
    )
    row = blank_db.execute(
        "SELECT received_at FROM dash0_spans WHERE trace_id='t1' AND span_id='s1'"
    ).fetchone()
    assert row["received_at"] is not None
    assert len(row["received_at"]) > 0


def test_016_full_stack_apply(migrated_db: sqlite3.Connection):
    """Full migration stack including 016 applies cleanly."""
    tables = {
        row[0]
        for row in migrated_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"sessions", "turns", "tool_calls"} <= tables
    assert {"dash0_resources", "dash0_spans", "dash0_span_events", "dash0_log_records"} <= tables
    versions = {
        row[0]
        for row in migrated_db.execute("SELECT version FROM schema_version").fetchall()
    }
    assert 16 in versions
