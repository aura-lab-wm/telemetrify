"""End-to-end: POST OTLP -> rows in dash0_* -> /dash0/health reflects them."""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_on_migrated_db(monkeypatch, migrated_db: sqlite3.Connection):
    """Build a FastAPI app whose connect() returns the migrated_db fixture."""
    import telemetrify.dash0.receiver as receiver_mod

    monkeypatch.setattr(receiver_mod, "connect", lambda: migrated_db)

    app = FastAPI()
    app.include_router(receiver_mod.router)
    return TestClient(app), migrated_db


def _one_span() -> dict[str, Any]:
    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "claude-code"}},
            ]},
            "scopeSpans": [{
                "scope": {"name": "dash0", "version": "0.1"},
                "spans": [{
                    "traceId": "trace01",
                    "spanId":  "span01",
                    "name": "tool.Bash",
                    "kind": 1,
                    "startTimeUnixNano": "1700000000000000000",
                    "endTimeUnixNano":   "1700000000100000000",
                    "attributes": [
                        {"key": "gen_ai.conversation.id", "value": {"stringValue": "sess-e2e"}},
                    ],
                    "status": {"code": 1},
                }],
            }],
        }],
    }


def _one_log() -> dict[str, Any]:
    return {
        "resourceLogs": [{
            "resource": {"attributes": []},
            "scopeLogs": [{
                "scope": {"name": "dash0"},
                "logRecords": [{
                    "timeUnixNano": "1700000000000000000",
                    "severityNumber": 9,
                    "severityText": "INFO",
                    "body": {"stringValue": "hello from e2e"},
                }],
            }],
        }],
    }


def test_post_traces_persists_rows(app_on_migrated_db):
    client, db = app_on_migrated_db
    r = client.post("/v1/traces", json=_one_span())
    assert r.status_code == 200
    n = db.execute("SELECT COUNT(*) FROM dash0_spans").fetchone()[0]
    assert n == 1
    row = db.execute("SELECT name, conversation_id FROM dash0_spans").fetchone()
    assert row["name"] == "tool.Bash"
    assert row["conversation_id"] == "sess-e2e"


def test_post_logs_persists_rows(app_on_migrated_db):
    client, db = app_on_migrated_db
    r = client.post("/v1/logs", json=_one_log())
    assert r.status_code == 200
    n = db.execute("SELECT COUNT(*) FROM dash0_log_records").fetchone()[0]
    assert n == 1


def test_dash0_health_reflects_inserts(app_on_migrated_db):
    client, _ = app_on_migrated_db
    # Before any POST: zeros + null timestamps.
    r0 = client.get("/dash0/health")
    assert r0.status_code == 200
    h0 = r0.json()
    assert h0["ok"] is True
    assert h0["spans_total"] == 0
    assert h0["logs_total"] == 0
    assert h0["last_span_received_at"] is None

    # After POSTs: counts tick.
    client.post("/v1/traces", json=_one_span())
    client.post("/v1/logs", json=_one_log())
    h1 = client.get("/dash0/health").json()
    assert h1["spans_total"] == 1
    assert h1["logs_total"] == 1
    assert h1["last_span_received_at"] is not None


def test_idempotent_post_does_not_duplicate(app_on_migrated_db):
    client, db = app_on_migrated_db
    body = _one_span()
    client.post("/v1/traces", json=body)
    client.post("/v1/traces", json=body)
    n = db.execute("SELECT COUNT(*) FROM dash0_spans").fetchone()[0]
    assert n == 1
