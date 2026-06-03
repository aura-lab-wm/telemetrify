"""Unit tests for telemetrify.rocco_sync — local→Rocco DB replication.

Design: every network/disk seam is a module-level function the tests
monkeypatch — `_run` (the single subprocess wrapper), `probe_connected`,
`push`, `make_snapshot`, `active_session_seconds`, and the `STATE_PATH`
global. No real ssh/rsync/sqlite-backup is ever invoked.

Covered:
  - active_session_seconds() sums per-session (max(finished)-min(started))
  - SyncState save/load round-trip
  - decide() branches: offline / bootstrap / retry / reconnect / active-2h / hold
  - tick() pushes on reconnect, holds below threshold, records offline
  - push() snapshots → rsyncs → writes a manifest
  - restore() no-ops when the local DB is present, pulls when it's absent
  - probe_connected() maps ssh exit code → bool
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest


def _seed_turns_db(path: Path, rows: list[tuple]) -> None:
    """rows: (session_id, started_at, finished_at)."""
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT, "
              "started_at TEXT, finished_at TEXT)")
    c.executemany("INSERT INTO turns(session_id, started_at, finished_at) VALUES (?,?,?)",
                  rows)
    c.commit()
    c.close()


def _fake_run(record, *, rc=0, side_effect=None):
    """Return a fake `_run` that records argv and returns a CompletedProcess."""
    def run(cmd, timeout=None):
        record.append(list(cmd))
        if side_effect is not None:
            side_effect(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")
    return run


# ── active session time ──────────────────────────────────────────────────
def test_active_session_seconds_sums_per_session(tmp_path):
    from telemetrify import rocco_sync as rs

    db = tmp_path / "t.db"
    _seed_turns_db(db, [
        # session A: 00:00:00 → 00:10:00  = 600s (two turns inside the span)
        ("A", "2026-06-03T00:00:00.000Z", "2026-06-03T00:05:00.000Z"),
        ("A", "2026-06-03T00:05:00.000Z", "2026-06-03T00:10:00.000Z"),
        # session B: 01:00:00 → 01:01:30  = 90s
        ("B", "2026-06-03T01:00:00.000Z", "2026-06-03T01:01:30.000Z"),
        # NULL session ignored
        (None, "2026-06-03T02:00:00.000Z", "2026-06-03T02:30:00.000Z"),
    ])
    conn = sqlite3.connect(str(db))
    assert rs.active_session_seconds(conn) == 600 + 90
    conn.close()


# ── state round-trip ─────────────────────────────────────────────────────
def test_state_roundtrip(tmp_path):
    from telemetrify import rocco_sync as rs

    p = tmp_path / "state.json"
    st = rs.SyncState(last_push_finished_at="2026-06-03T00:00:00+00:00",
                      last_push_ok=True, active_seconds_at_last_push=4242,
                      prev_connected=True)
    rs.save_state(st, p)
    got = rs.load_state(p)
    assert got == st


def test_load_state_missing_file_is_default(tmp_path):
    from telemetrify import rocco_sync as rs
    st = rs.load_state(tmp_path / "nope.json")
    assert st == rs.SyncState()


# ── decision logic ───────────────────────────────────────────────────────
def test_decide_offline_never_pushes():
    from telemetrify import rocco_sync as rs
    st = rs.SyncState(last_push_finished_at="x", last_push_ok=True, prev_connected=True)
    should, reason = rs.decide(st, connected=False, active_now=10**9)
    assert should is False and reason == "offline"


def test_decide_bootstrap_when_never_pushed():
    from telemetrify import rocco_sync as rs
    should, reason = rs.decide(rs.SyncState(), connected=True, active_now=0)
    assert should is True and reason == "bootstrap"


def test_decide_retry_after_failed_push():
    from telemetrify import rocco_sync as rs
    st = rs.SyncState(last_push_finished_at="x", last_push_ok=False, prev_connected=True)
    should, reason = rs.decide(st, connected=True, active_now=0)
    assert should is True and reason == "retry"


def test_decide_reconnect_edge():
    from telemetrify import rocco_sync as rs
    st = rs.SyncState(last_push_finished_at="x", last_push_ok=True, prev_connected=False)
    should, reason = rs.decide(st, connected=True, active_now=0)
    assert should is True and reason == "reconnect"


def test_decide_active_two_hours():
    from telemetrify import rocco_sync as rs
    st = rs.SyncState(last_push_finished_at="x", last_push_ok=True,
                      prev_connected=True, active_seconds_at_last_push=1000)
    # exactly 2h more of active time
    should, reason = rs.decide(st, connected=True, active_now=1000 + 7200)
    assert should is True and reason == "active-2h"


def test_decide_holds_below_threshold():
    from telemetrify import rocco_sync as rs
    st = rs.SyncState(last_push_finished_at="x", last_push_ok=True,
                      prev_connected=True, active_seconds_at_last_push=1000)
    should, reason = rs.decide(st, connected=True, active_now=1000 + 7199)
    assert should is False


# ── tick orchestration ───────────────────────────────────────────────────
def test_tick_pushes_on_reconnect(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs

    state_p = tmp_path / "state.json"
    monkeypatch.setattr(rs, "STATE_PATH", state_p)
    rs.save_state(rs.SyncState(last_push_finished_at="old", last_push_ok=True,
                               prev_connected=False, active_seconds_at_last_push=50),
                  state_p)

    db = tmp_path / "t.db"; _seed_turns_db(db, [])
    monkeypatch.setattr(rs, "probe_connected", lambda *a, **k: True)
    monkeypatch.setattr(rs, "active_session_seconds", lambda conn: 12345)
    pushed = {}
    def fake_push():
        pushed["yes"] = True
        return {"rows": 1, "bytes": 1, "finished_at": "2026-06-03T03:00:00+00:00"}
    monkeypatch.setattr(rs, "push", fake_push)

    res = rs.tick(db_path=db)
    assert res["pushed"] is True
    assert res["decision"] == "reconnect"
    assert pushed.get("yes") is True

    st = rs.load_state(state_p)
    assert st.prev_connected is True
    assert st.active_seconds_at_last_push == 12345
    assert st.last_push_ok is True


def test_tick_holds_below_threshold(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs

    state_p = tmp_path / "state.json"
    monkeypatch.setattr(rs, "STATE_PATH", state_p)
    rs.save_state(rs.SyncState(last_push_finished_at="old", last_push_ok=True,
                               prev_connected=True, active_seconds_at_last_push=100),
                  state_p)
    db = tmp_path / "t.db"; _seed_turns_db(db, [])
    monkeypatch.setattr(rs, "probe_connected", lambda *a, **k: True)
    monkeypatch.setattr(rs, "active_session_seconds", lambda conn: 200)  # +100s only
    monkeypatch.setattr(rs, "push", lambda: (_ for _ in ()).throw(AssertionError("push!")))

    res = rs.tick(db_path=db)
    assert res["pushed"] is False


def test_tick_offline_records_disconnect(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs

    state_p = tmp_path / "state.json"
    monkeypatch.setattr(rs, "STATE_PATH", state_p)
    rs.save_state(rs.SyncState(last_push_finished_at="old", last_push_ok=True,
                               prev_connected=True), state_p)
    db = tmp_path / "t.db"; _seed_turns_db(db, [])
    monkeypatch.setattr(rs, "probe_connected", lambda *a, **k: False)
    monkeypatch.setattr(rs, "active_session_seconds", lambda conn: 0)
    monkeypatch.setattr(rs, "push", lambda: (_ for _ in ()).throw(AssertionError("push!")))

    res = rs.tick(db_path=db)
    assert res["pushed"] is False
    assert rs.load_state(state_p).prev_connected is False


# ── push ─────────────────────────────────────────────────────────────────
def test_push_snapshots_rsyncs_and_writes_manifest(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs

    db = tmp_path / "prompts.db"; _seed_turns_db(db, [("A", "x", "y")])
    monkeypatch.setattr(rs, "DB_PATH", db)
    snap = tmp_path / "snap" / "prompts.db"
    monkeypatch.setattr(rs, "SNAPSHOT_PATH", snap)

    def fake_snapshot(db_path=None, dest=None):
        d = Path(dest or snap); d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"snapshot-bytes")
        return d
    monkeypatch.setattr(rs, "make_snapshot", fake_snapshot)
    monkeypatch.setenv("ROCCO_SSH_HOST", "rocco")
    monkeypatch.setenv("ROCCO_STORE_DIR", "~/telemetrify-store")

    calls: list[list[str]] = []
    monkeypatch.setattr(rs, "_run", _fake_run(calls, rc=0))

    manifest = rs.push()
    assert manifest["rows"] == 1
    # an rsync to the remote prompts.db happened
    rsyncs = [c for c in calls if c and c[0] == "rsync"]
    assert any("rocco:~/telemetrify-store/prompts.db" in c[-1] for c in rsyncs)
    # a manifest file was written locally
    assert (snap.parent / "manifest.json").exists()


def test_push_raises_when_rsync_fails(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs

    db = tmp_path / "prompts.db"; _seed_turns_db(db, [])
    monkeypatch.setattr(rs, "DB_PATH", db)
    monkeypatch.setattr(rs, "make_snapshot",
                        lambda db_path=None, dest=None: db)  # reuse existing file

    def run(cmd, timeout=None):
        # ssh mkdir ok, rsync fails
        rc = 0 if cmd[0] == "ssh" else 23
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="rsync boom")
    monkeypatch.setattr(rs, "_run", run)

    with pytest.raises(rs.RoccoSyncError):
        rs.push()


# ── restore ──────────────────────────────────────────────────────────────
def test_restore_noop_when_local_present(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs

    db = tmp_path / "prompts.db"; db.write_bytes(b"x" * 100)
    monkeypatch.setattr(rs, "DB_PATH", db)
    calls: list = []
    monkeypatch.setattr(rs, "_run", _fake_run(calls, rc=0))

    res = rs.restore(db_path=db)
    assert res["restored"] is False
    assert calls == [], "restore must not touch the network when local DB is present"


def test_restore_pulls_when_absent(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs

    db = tmp_path / "prompts.db"  # does not exist
    monkeypatch.setattr(rs, "DB_PATH", db)
    monkeypatch.setenv("ROCCO_SSH_HOST", "rocco")
    monkeypatch.setenv("ROCCO_STORE_DIR", "~/telemetrify-store")

    calls: list = []
    def side(cmd):
        # rsync writes to its DEST (last argv element) — a temp path, not the
        # final DB. restore() atomically renames it into place on success.
        Path(cmd[-1]).write_bytes(b"pulled" * 10)
    monkeypatch.setattr(rs, "_run", _fake_run(calls, rc=0, side_effect=side))

    res = rs.restore(db_path=db)
    assert res["restored"] is True
    assert db.exists() and db.read_bytes() == b"pulled" * 10
    rsyncs = [c for c in calls if c and c[0] == "rsync"]
    assert any("rocco:~/telemetrify-store/prompts.db" in c for c in rsyncs)


def test_restore_interrupted_leaves_no_partial_db(tmp_path, monkeypatch):
    """A failed/interrupted rsync must NOT leave a truncated prompts.db in
    place (which would look 'present' and permanently block re-restore while
    serving a corrupt DB). The download goes to a temp file; on failure the
    final DB path stays absent."""
    from telemetrify import rocco_sync as rs

    db = tmp_path / "prompts.db"  # absent
    monkeypatch.setattr(rs, "DB_PATH", db)
    monkeypatch.setenv("ROCCO_SSH_HOST", "rocco")
    monkeypatch.setenv("ROCCO_STORE_DIR", "~/telemetrify-store")

    calls: list = []
    def side(cmd):
        Path(cmd[-1]).write_bytes(b"halfway")  # partial temp download

    monkeypatch.setattr(rs, "_run", _fake_run(calls, rc=30, side_effect=side))

    res = rs.restore(db_path=db)
    assert res["restored"] is False
    assert not db.exists(), "a failed restore must not leave a partial prompts.db"
    # and a later successful restore can still proceed (temp was cleaned up)
    leftovers = list(tmp_path.glob("prompts.db*"))
    assert leftovers == [], f"temp download not cleaned up: {leftovers}"


# ── connectivity probe ───────────────────────────────────────────────────
def test_probe_connected_maps_exit_code(monkeypatch):
    from telemetrify import rocco_sync as rs

    monkeypatch.setattr(rs, "_run",
                        lambda cmd, timeout=None: subprocess.CompletedProcess(cmd, 0, "", ""))
    assert rs.probe_connected("rocco", []) is True

    monkeypatch.setattr(rs, "_run",
                        lambda cmd, timeout=None: subprocess.CompletedProcess(cmd, 255, "", ""))
    assert rs.probe_connected("rocco", []) is False

    def boom(cmd, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(rs, "_run", boom)
    assert rs.probe_connected("rocco", []) is False
