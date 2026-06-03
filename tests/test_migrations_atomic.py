"""A .sql migration's DDL must be atomic with its schema_version ledger insert.

Previously _apply_one ran `executescript(sql)` (DDL auto-commits) and THEN a
separate ledger INSERT + commit(). A crash in that gap left the schema changed
but unrecorded, so the next run re-applied the migration → "table already
exists" / double DML. The fix wraps DDL + ledger insert in one BEGIN…COMMIT.
"""
from __future__ import annotations

import sqlite3

import pytest


def test_sql_migration_writes_ddl_and_ledger_atomically(blank_db, tmp_path):
    from telemetrify.migrations import _runner as R

    conn = blank_db
    conn.executescript(R.LEDGER_SQL)

    mig = tmp_path / "999_atomic_ok.sql"
    mig.write_text("CREATE TABLE atomictest (x INTEGER);")

    R._apply_one(conn, 999, "atomic_ok", mig, log=lambda _m: None)

    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='atomictest'"
    ).fetchone(), "DDL must be applied"
    assert conn.execute(
        "SELECT 1 FROM schema_version WHERE version=999"
    ).fetchone(), "ledger row must be written"


def test_sql_migration_failure_rolls_back_ddl_and_ledger(blank_db, tmp_path):
    """If any statement in the migration fails, NOTHING is committed — neither
    a half-applied DDL nor the ledger row. (Old code auto-committed the first
    CREATE before the failure, leaving an orphan table.)"""
    from telemetrify.migrations import _runner as R

    conn = blank_db
    conn.executescript(R.LEDGER_SQL)

    mig = tmp_path / "998_atomic_bad.sql"
    # The second statement fails (duplicate table) AFTER the first DDL.
    mig.write_text("CREATE TABLE half (x INTEGER);\nCREATE TABLE half (x INTEGER);")

    with pytest.raises(sqlite3.Error):
        R._apply_one(conn, 998, "atomic_bad", mig, log=lambda _m: None)
    conn.rollback()  # mirrors apply()'s exception handler

    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='half'"
    ).fetchone() is None, "the first CREATE must NOT have auto-committed"
    assert conn.execute(
        "SELECT 1 FROM schema_version WHERE version=998"
    ).fetchone() is None, "no ledger row for a failed migration"
