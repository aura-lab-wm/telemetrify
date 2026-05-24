"""HTTP contract tests for the OTLP/HTTP receiver router."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _NoopConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass


@pytest.fixture
def client_and_calls(monkeypatch):
    """TestClient with the store + db.connect patched out so requests don't touch disk."""
    calls: dict[str, list[Any]] = {"traces": [], "logs": []}

    def fake_insert_trace(conn, payload, *, raw_body=None):
        calls["traces"].append((payload, raw_body))
        return len(payload.get("resourceSpans") or [])

    def fake_insert_log(conn, payload):
        calls["logs"].append(payload)
        return len(payload.get("resourceLogs") or [])

    import prompt_telemetry.dash0.receiver as receiver_mod
    import prompt_telemetry.dash0.store as store_mod

    monkeypatch.setattr(store_mod, "insert_trace_export", fake_insert_trace)
    monkeypatch.setattr(store_mod, "insert_log_export", fake_insert_log)
    monkeypatch.setattr(receiver_mod, "insert_trace_export", fake_insert_trace)
    monkeypatch.setattr(receiver_mod, "insert_log_export", fake_insert_log)
    monkeypatch.setattr(receiver_mod, "connect", lambda: _NoopConn())

    app = FastAPI()
    app.include_router(receiver_mod.router)
    return TestClient(app), calls


def _trace_export_one_span() -> dict[str, Any]:
    return {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-code"}},
                ],
            },
            "scopeSpans": [{
                "scope": {"name": "dash0/test", "version": "0.0.1"},
                "spans": [{
                    "traceId": "0102030405060708090a0b0c0d0e0f10",
                    "spanId": "1112131415161718",
                    "name": "tool.Bash",
                    "kind": 1,
                    "startTimeUnixNano": "1700000000000000000",
                    "endTimeUnixNano":   "1700000000100000000",
                    "attributes": [
                        {"key": "gen_ai.conversation.id", "value": {"stringValue": "sess-abc"}},
                        {"key": "gen_ai.tool.name",       "value": {"stringValue": "Bash"}},
                    ],
                    "status": {"code": 1},
                }],
            }],
        }],
    }


def _log_export_one_record() -> dict[str, Any]:
    return {
        "resourceLogs": [{
            "resource": {"attributes": []},
            "scopeLogs": [{
                "scope": {"name": "dash0/test"},
                "logRecords": [{
                    "timeUnixNano": "1700000000000000000",
                    "severityNumber": 9,
                    "severityText": "INFO",
                    "body": {"stringValue": "hello"},
                }],
            }],
        }],
    }


# ─── /v1/traces ───────────────────────────────────────────────────────

def test_traces_json_ok_returns_partial_success_ack(client_and_calls):
    client, calls = client_and_calls
    resp = client.post("/v1/traces", json=_trace_export_one_span())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"partialSuccess": {"rejectedSpans": "0"}}
    assert len(calls["traces"]) == 1
    payload, raw = calls["traces"][0]
    assert payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "tool.Bash"
    assert raw is not None and len(raw) > 0


def test_traces_empty_body_400(client_and_calls):
    client, _ = client_and_calls
    resp = client.post("/v1/traces", content=b"", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert "empty body" in resp.text


def test_traces_invalid_json_400(client_and_calls):
    client, _ = client_and_calls
    resp = client.post(
        "/v1/traces",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert "invalid JSON" in resp.text


def test_traces_protobuf_returns_415_with_actionable_message(client_and_calls):
    client, _ = client_and_calls
    resp = client.post(
        "/v1/traces",
        content=b"\x00\x01\x02",
        headers={"content-type": "application/x-protobuf"},
    )
    assert resp.status_code == 415
    body = resp.text.lower()
    assert "protobuf" in body
    assert "json" in body or "opentelemetry-proto" in body


def test_traces_unsupported_content_type_415(client_and_calls):
    client, _ = client_and_calls
    resp = client.post(
        "/v1/traces",
        content=b"foo",
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 415


def test_traces_charset_suffix_in_content_type_is_tolerated(client_and_calls):
    client, calls = client_and_calls
    resp = client.post(
        "/v1/traces",
        content=json.dumps(_trace_export_one_span()).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200
    assert len(calls["traces"]) == 1


# ─── /v1/logs ─────────────────────────────────────────────────────────

def test_logs_json_ok(client_and_calls):
    client, calls = client_and_calls
    resp = client.post("/v1/logs", json=_log_export_one_record())
    assert resp.status_code == 200
    assert resp.json() == {"partialSuccess": {"rejectedLogRecords": "0"}}
    assert len(calls["logs"]) == 1


def test_logs_empty_400(client_and_calls):
    client, _ = client_and_calls
    resp = client.post("/v1/logs", content=b"", headers={"content-type": "application/json"})
    assert resp.status_code == 400


# ─── /v1/metrics ──────────────────────────────────────────────────────

def test_metrics_accepted_and_dropped(client_and_calls):
    client, calls = client_and_calls
    resp = client.post("/v1/metrics", json={"resourceMetrics": [{"foo": "bar"}]})
    assert resp.status_code == 200
    assert resp.json() == {"partialSuccess": {"rejectedDataPoints": "0"}}
    # No insert; metrics are dropped intentionally.
    assert calls["traces"] == []
    assert calls["logs"] == []
