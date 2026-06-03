"""Connection-lifecycle tests for telemetrify.db.connect().

Regression for the FD leak that wedged the UI (a long-running process climbed
past the open-file limit → SQLITE_CANTOPEN). connect() now returns a per-thread
cached connection, so file descriptors are bounded by the uvicorn threadpool
size instead of leaking one per request.

These tests stub _raw_connect (returns throwaway :memory: connections) and
migrations.apply so they exercise only the caching logic — no real DB / sqlite_vec.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest


def _stub_db(monkeypatch):
    from telemetrify import db
    import telemetrify.migrations as M

    # Fresh thread-local so prior tests can't pollute the cache.
    monkeypatch.setattr(db, "_local", threading.local())
    made = {"n": 0}

    def fake_raw(path):
        made["n"] += 1
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_raw_connect", fake_raw)
    monkeypatch.setattr(M, "apply", lambda conn, log=None: None)
    return db, made


def test_connect_caches_per_thread(tmp_path, monkeypatch):
    db, made = _stub_db(monkeypatch)
    p = tmp_path / "x.db"
    c1 = db.connect(p)
    c2 = db.connect(p)
    assert c1 is c2, "same thread + same path must reuse one connection"
    assert made["n"] == 1, "must open (and migrate) exactly once, not per call"


def test_connect_reopens_after_close(tmp_path, monkeypatch):
    db, made = _stub_db(monkeypatch)
    p = tmp_path / "x.db"
    c1 = db.connect(p)
    c1.close()  # a caller closed the cached connection
    c2 = db.connect(p)
    assert c2 is not c1, "a closed cached connection must be transparently reopened"
    assert c2.execute("SELECT 1").fetchone()[0] == 1
    assert made["n"] == 2


def test_connect_distinct_per_thread(tmp_path, monkeypatch):
    db, made = _stub_db(monkeypatch)
    p = tmp_path / "x.db"
    main_conn = db.connect(p)
    other: dict = {}

    def worker():
        other["conn"] = db.connect(p)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert other["conn"] is not main_conn, "each thread gets its own connection"
    assert made["n"] == 2
