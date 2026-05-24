"""/api/pulse and /start smoke tests against a migrated empty DB."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_on_migrated_db(monkeypatch, migrated_db: sqlite3.Connection):
    import telemetrify.ui.app as app_mod

    monkeypatch.setattr(app_mod, "connect", lambda: migrated_db)
    return TestClient(app_mod.app), migrated_db


def _insert_turn(db, turn_id: int, started_at: str, model: str = "claude-opus-4-7",
                 user_text: str = "test prompt", cwd: str = "/Users/x/Projects/telemetrify",
                 input_tokens: int = 100, output_tokens: int = 200) -> None:
    # sessions row first (FK)
    db.execute(
        """INSERT OR IGNORE INTO sessions(id, started_at, last_turn_at, cwd)
           VALUES(?, ?, ?, ?)""",
        (f"sess-{turn_id}", started_at, started_at, cwd),
    )
    db.execute(
        """INSERT INTO turns(id, session_id, started_at, finished_at, model,
                              user_text, assistant_text, input_tokens, output_tokens, cwd)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (turn_id, f"sess-{turn_id}", started_at, started_at, model,
         user_text, "ok", input_tokens, output_tokens, cwd),
    )
    db.commit()


def test_pulse_shape_with_empty_db(client_on_migrated_db):
    client, _ = client_on_migrated_db
    r = client.get("/api/pulse")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {
        "server_now", "last_turn_at", "minutes_since_last_turn", "is_live",
        "window_minutes", "last_hour", "last_24h", "recent_turns",
    }
    assert body["last_turn_at"] is None
    assert body["is_live"] is False
    assert len(body["last_24h"]["sparkline"]) == 24
    assert body["recent_turns"] == []
    assert body["last_hour"]["turns"] == 0


def test_pulse_reflects_recent_turn(client_on_migrated_db):
    client, db = client_on_migrated_db
    # one turn 30 minutes ago (live window is 5 min, so is_live=False)
    _insert_turn(db, 1, db.execute("SELECT datetime('now', '-30 minutes')").fetchone()[0])
    # one turn 1 minute ago — should flip is_live=True
    _insert_turn(db, 2, db.execute("SELECT datetime('now', '-1 minutes')").fetchone()[0])

    r = client.get("/api/pulse")
    assert r.status_code == 200
    body = r.json()
    assert body["is_live"] is True
    assert body["last_hour"]["turns"] == 2
    assert body["last_hour"]["tokens"] == 600
    assert body["last_hour"]["top_model"] == "claude-opus-4-7"
    assert body["last_hour"]["top_cwd_basename"] == "telemetrify"
    assert len(body["recent_turns"]) == 2
    assert body["recent_turns"][0]["id"] == 2  # newest first


def test_start_renders(client_on_migrated_db):
    client, _ = client_on_migrated_db
    r = client.get("/start")
    assert r.status_code == 200
    body = r.text
    assert "What is" in body
    assert "30-second tour" in body or "No turns captured yet" in body
    # has the navigation cards
    assert "/clusters" in body
    assert "/ask" in body
    assert "/dashboard" in body
