"""OTLP -> dash0_* shredder unit tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from telemetrify.dash0.store import (
    _attr_value,
    _fingerprint,
    _kv_array_to_dict,
    insert_log_export,
    insert_trace_export,
)


MIGRATION_016 = (
    Path(__file__).resolve().parents[2]
    / "src" / "telemetrify" / "migrations" / "016_dash0_otel.sql"
)


def _apply_dash0_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATION_016.read_text(encoding="utf-8"))


# ─── attribute decoding ───────────────────────────────────────────────

def test_attr_value_string():
    assert _attr_value({"stringValue": "hello"}) == "hello"


def test_attr_value_int_as_string():
    # OTLP/HTTP JSON encodes int64 as string to dodge JS precision loss.
    assert _attr_value({"intValue": "42"}) == 42


def test_attr_value_int_as_number():
    assert _attr_value({"intValue": 42}) == 42


def test_attr_value_bool():
    assert _attr_value({"boolValue": True}) is True


def test_attr_value_double():
    assert _attr_value({"doubleValue": 3.14}) == 3.14


def test_attr_value_array():
    val = {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}}
    assert _attr_value(val) == ["a", "b"]


def test_attr_value_kvlist():
    val = {"kvlistValue": {"values": [{"key": "k", "value": {"stringValue": "v"}}]}}
    assert _attr_value(val) == {"k": "v"}


def test_attr_value_empty_returns_none():
    assert _attr_value({}) is None


def test_kv_array_skips_keyless():
    kvs = [
        {"key": "ok", "value": {"stringValue": "v"}},
        {"value": {"stringValue": "no-key"}},
    ]
    assert _kv_array_to_dict(kvs) == {"ok": "v"}


def test_fingerprint_is_key_order_stable():
    a = {"x": 1, "y": "z", "nested": {"b": 2, "a": 1}}
    b = {"nested": {"a": 1, "b": 2}, "y": "z", "x": 1}
    assert _fingerprint(a) == _fingerprint(b)


# ─── trace shredding ──────────────────────────────────────────────────

def _one_span_payload(trace_id="aa", span_id="bb", conv="sess-1", name="tool.Bash"):
    return {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-code"}},
                ],
            },
            "scopeSpans": [{
                "scope": {"name": "dash0", "version": "0.1"},
                "spans": [{
                    "traceId": trace_id,
                    "spanId": span_id,
                    "name": name,
                    "kind": 1,
                    "startTimeUnixNano": "1700000000000000000",
                    "endTimeUnixNano":   "1700000000100000000",
                    "attributes": [
                        {"key": "gen_ai.conversation.id", "value": {"stringValue": conv}},
                        {"key": "gen_ai.tool.name", "value": {"stringValue": "Bash"}},
                    ],
                    "status": {"code": 1},
                }],
            }],
        }],
    }


def test_insert_one_span_records_all_fields(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    n = insert_trace_export(blank_db, _one_span_payload())
    assert n == 1
    row = blank_db.execute("SELECT * FROM dash0_spans").fetchone()
    assert row["trace_id"] == "aa"
    assert row["span_id"] == "bb"
    assert row["conversation_id"] == "sess-1"
    assert row["name"] == "tool.Bash"
    assert row["kind"] == 1
    assert row["start_ns"] == 1700000000000000000
    assert row["end_ns"] == 1700000000100000000
    assert row["status_code"] == 1
    assert row["scope_name"] == "dash0"
    assert row["scope_version"] == "0.1"
    attrs = json.loads(row["attrs_json"])
    assert attrs["gen_ai.tool.name"] == "Bash"


def test_insert_is_idempotent_on_trace_span_pk(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_span_payload()
    n1 = insert_trace_export(blank_db, payload)
    n2 = insert_trace_export(blank_db, payload)
    assert n1 == 1
    assert n2 == 0
    cnt = blank_db.execute("SELECT COUNT(*) FROM dash0_spans").fetchone()[0]
    assert cnt == 1


def test_resources_dedup_by_fingerprint(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    insert_trace_export(blank_db, _one_span_payload(trace_id="t1", span_id="s1"))
    insert_trace_export(blank_db, _one_span_payload(trace_id="t2", span_id="s2"))
    assert blank_db.execute("SELECT COUNT(*) FROM dash0_resources").fetchone()[0] == 1


def test_distinct_resource_attrs_create_distinct_rows(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    insert_trace_export(blank_db, _one_span_payload(trace_id="t1", span_id="s1"))
    other = _one_span_payload(trace_id="t2", span_id="s2")
    other["resourceSpans"][0]["resource"]["attributes"] = [
        {"key": "service.name", "value": {"stringValue": "OTHER-AGENT"}},
    ]
    insert_trace_export(blank_db, other)
    assert blank_db.execute("SELECT COUNT(*) FROM dash0_resources").fetchone()[0] == 2


def test_span_events_recorded_with_attrs(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_span_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"] = [
        {"timeUnixNano": "1700000000050000000", "name": "tool.input",
         "attributes": [{"key": "size", "value": {"intValue": "123"}}]},
        {"timeUnixNano": "1700000000080000000", "name": "tool.output", "attributes": []},
    ]
    insert_trace_export(blank_db, payload)
    evs = blank_db.execute("SELECT * FROM dash0_span_events ORDER BY seq").fetchall()
    assert len(evs) == 2
    assert evs[0]["seq"] == 0
    assert evs[0]["name"] == "tool.input"
    assert evs[0]["time_ns"] == 1700000000050000000
    assert json.loads(evs[0]["attrs_json"])["size"] == 123
    assert evs[1]["seq"] == 1
    assert evs[1]["name"] == "tool.output"
    assert evs[1]["attrs_json"] is None


def test_status_message_recorded(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_span_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"] = {
        "code": 2, "message": "boom: io error",
    }
    insert_trace_export(blank_db, payload)
    row = blank_db.execute("SELECT status_code, status_message FROM dash0_spans").fetchone()
    assert row["status_code"] == 2
    assert row["status_message"] == "boom: io error"


def test_no_resource_attrs_inserts_span_with_null_resource_id(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_span_payload()
    payload["resourceSpans"][0]["resource"]["attributes"] = []
    assert insert_trace_export(blank_db, payload) == 1
    row = blank_db.execute("SELECT resource_id FROM dash0_spans").fetchone()
    assert row["resource_id"] is None


def test_span_with_missing_ids_is_skipped(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_span_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = ""
    assert insert_trace_export(blank_db, payload) == 0


def test_multiple_scope_spans_in_one_export(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_span_payload()
    payload["resourceSpans"][0]["scopeSpans"].append({
        "scope": {"name": "dash0/other", "version": "9.9"},
        "spans": [{
            "traceId": "aa",
            "spanId": "cc",
            "name": "tool.Read",
            "startTimeUnixNano": "1700000000200000000",
        }],
    })
    assert insert_trace_export(blank_db, payload) == 2
    names = [
        row[0]
        for row in blank_db.execute("SELECT name FROM dash0_spans ORDER BY name").fetchall()
    ]
    assert names == ["tool.Bash", "tool.Read"]


def test_parent_span_id_recorded(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_span_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["parentSpanId"] = "deadbeef"
    insert_trace_export(blank_db, payload)
    row = blank_db.execute("SELECT parent_span_id FROM dash0_spans").fetchone()
    assert row["parent_span_id"] == "deadbeef"


# ─── log shredding ────────────────────────────────────────────────────

def _one_log_payload(conv="sess-1", body="hello", with_trace_ctx=False):
    rec: dict = {
        "timeUnixNano": "1700000000000000000",
        "observedTimeUnixNano": "1700000000000000001",
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": body},
        "attributes": [
            {"key": "gen_ai.conversation.id", "value": {"stringValue": conv}},
        ],
    }
    if with_trace_ctx:
        rec["traceId"] = "tracehex"
        rec["spanId"] = "spanhex"
    return {
        "resourceLogs": [{
            "resource": {"attributes": []},
            "scopeLogs": [{
                "scope": {"name": "dash0"},
                "logRecords": [rec],
            }],
        }]
    }


def test_log_record_inserted_with_all_fields(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    n = insert_log_export(blank_db, _one_log_payload(with_trace_ctx=True))
    assert n == 1
    row = blank_db.execute("SELECT * FROM dash0_log_records").fetchone()
    assert row["body_text"] == "hello"
    assert row["conversation_id"] == "sess-1"
    assert row["severity_text"] == "INFO"
    assert row["severity_number"] == 9
    assert row["time_ns"] == 1700000000000000000
    assert row["observed_time_ns"] == 1700000000000000001
    assert row["trace_id"] == "tracehex"
    assert row["span_id"] == "spanhex"


def test_log_without_trace_context_inserts_with_nulls(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_log_payload(with_trace_ctx=False)
    insert_log_export(blank_db, payload)
    row = blank_db.execute("SELECT trace_id, span_id FROM dash0_log_records").fetchone()
    assert row["trace_id"] is None
    assert row["span_id"] is None


def test_log_body_kvlist_is_json_stringified(blank_db: sqlite3.Connection):
    _apply_dash0_schema(blank_db)
    payload = _one_log_payload()
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["body"] = {
        "kvlistValue": {"values": [
            {"key": "msg", "value": {"stringValue": "structured"}},
        ]},
    }
    insert_log_export(blank_db, payload)
    body = blank_db.execute("SELECT body_text FROM dash0_log_records").fetchone()["body_text"]
    parsed = json.loads(body)
    assert parsed == {"msg": "structured"}


# ─── join back to sessions ────────────────────────────────────────────

def test_join_dash0_span_to_session_via_conversation_id(migrated_db: sqlite3.Connection):
    migrated_db.execute(
        "INSERT INTO sessions(id, started_at, cwd) "
        "VALUES ('sess-1', '2026-05-24T00:00:00Z', '/tmp')"
    )
    insert_trace_export(migrated_db, _one_span_payload(conv="sess-1"))
    row = migrated_db.execute("""
        SELECT s.id AS sid, d.trace_id, d.name AS span_name
        FROM sessions s
        JOIN dash0_spans d ON d.conversation_id = s.id
        WHERE s.id = 'sess-1'
    """).fetchone()
    assert row is not None
    assert row["sid"] == "sess-1"
    assert row["span_name"] == "tool.Bash"
