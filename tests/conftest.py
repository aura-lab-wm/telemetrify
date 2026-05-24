"""Shared pytest fixtures.

`blank_db`    — a fresh sqlite-vec-enabled DB with no migrations applied.
`migrated_db` — same, with the full migration stack applied. Monkeypatches the
                runner's DATA_DIR/DB_PATH/BACKUPS_DIR so the operator's real
                936 MB DB is never touched by per-migration backup logic.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
import sqlite_vec


def _open_blank(path: Path) -> sqlite3.Connection:
    # check_same_thread=False so e2e tests can share a fixture-owned connection
    # across the main thread and FastAPI's BackgroundTasks worker.
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.fixture
def blank_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = _open_blank(tmp_path / "test.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def migrated_db(tmp_path: Path, monkeypatch) -> Iterator[sqlite3.Connection]:
    import prompt_telemetry as pkg
    import prompt_telemetry.migrations._runner as runner

    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    tmp_backups = tmp_data / "backups"
    tmp_db_path = tmp_data / "prompts.db"

    monkeypatch.setattr(pkg, "DATA_DIR", tmp_data)
    monkeypatch.setattr(pkg, "DB_PATH", tmp_db_path)
    monkeypatch.setattr(runner, "DATA_DIR", tmp_data)
    monkeypatch.setattr(runner, "DB_PATH", tmp_db_path)
    monkeypatch.setattr(runner, "BACKUPS_DIR", tmp_backups)

    conn = _open_blank(tmp_db_path)
    runner.apply(conn, log=lambda _msg: None)
    try:
        yield conn
    finally:
        conn.close()
