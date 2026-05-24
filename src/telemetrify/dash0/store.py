"""OTLP/HTTP JSON exports -> dash0_* tables. Idempotent on (trace_id, span_id)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable


def _attr_value(value_obj: dict[str, Any]) -> Any:
    """OTel AnyValue -> Python primitive. Returns None if no field is set."""
    if not isinstance(value_obj, dict):
        return None
    if "stringValue" in value_obj:
        return value_obj["stringValue"]
    if "intValue" in value_obj:
        v = value_obj["intValue"]
        # OTLP/HTTP JSON encodes int64 as string per spec; tolerate raw ints too.
        return int(v) if isinstance(v, str) else v
    if "boolValue" in value_obj:
        return bool(value_obj["boolValue"])
    if "doubleValue" in value_obj:
        return float(value_obj["doubleValue"])
    if "arrayValue" in value_obj:
        return [_attr_value(x) for x in (value_obj["arrayValue"].get("values") or [])]
    if "kvlistValue" in value_obj:
        return _kv_array_to_dict(value_obj["kvlistValue"].get("values") or [])
    if "bytesValue" in value_obj:
        return value_obj["bytesValue"]
    return None


def _kv_array_to_dict(kvs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in kvs or []:
        k = kv.get("key")
        if not k:
            continue
        out[k] = _attr_value(kv.get("value") or {})
    return out


def _fingerprint(attrs: dict[str, Any]) -> str:
    canonical = json.dumps(attrs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _coerce_ns(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _upsert_resource(conn: sqlite3.Connection, resource_obj: dict[str, Any] | None) -> int | None:
    if not resource_obj:
        return None
    attrs = _kv_array_to_dict(resource_obj.get("attributes") or [])
    if not attrs:
        return None
    fp = _fingerprint(attrs)
    row = conn.execute("SELECT id FROM dash0_resources WHERE fingerprint = ?", (fp,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE dash0_resources SET last_seen_at = datetime('now') WHERE id = ?",
            (row[0],),
        )
        return row[0]
    cur = conn.execute(
        "INSERT INTO dash0_resources(fingerprint, attrs_json) VALUES (?, ?)",
        (fp, json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)),
    )
    return cur.lastrowid


def insert_trace_export(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    raw_body: bytes | None = None,
) -> int:
    """Walk ExportTraceServiceRequest. Returns count of newly-inserted spans."""
    inserted = 0
    for rs in payload.get("resourceSpans") or []:
        resource_id = _upsert_resource(conn, rs.get("resource"))
        for ss in rs.get("scopeSpans") or []:
            scope = ss.get("scope") or {}
            scope_name = scope.get("name")
            scope_version = scope.get("version")
            for span in ss.get("spans") or []:
                trace_id = span.get("traceId") or ""
                span_id = span.get("spanId") or ""
                if not trace_id or not span_id:
                    continue
                attrs = _kv_array_to_dict(span.get("attributes") or [])
                attrs_json = (
                    json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)
                    if attrs else None
                )
                status = span.get("status") or {}
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO dash0_spans(
                        trace_id, span_id, parent_span_id, conversation_id,
                        name, kind, start_ns, end_ns,
                        status_code, status_message, attrs_json,
                        resource_id, scope_name, scope_version
                    ) VALUES (?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?)
                    """,
                    (
                        trace_id, span_id, span.get("parentSpanId"),
                        attrs.get("gen_ai.conversation.id"),
                        span.get("name") or "",
                        span.get("kind"),
                        _coerce_ns(span.get("startTimeUnixNano")) or 0,
                        _coerce_ns(span.get("endTimeUnixNano")),
                        status.get("code"),
                        status.get("message"),
                        attrs_json,
                        resource_id, scope_name, scope_version,
                    ),
                )
                if cur.rowcount == 0:
                    continue
                inserted += 1
                for seq, ev in enumerate(span.get("events") or []):
                    ev_attrs = _kv_array_to_dict(ev.get("attributes") or [])
                    ev_attrs_json = (
                        json.dumps(ev_attrs, separators=(",", ":"), ensure_ascii=False)
                        if ev_attrs else None
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO dash0_span_events(
                            trace_id, span_id, seq, time_ns, name, attrs_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            trace_id, span_id, seq,
                            _coerce_ns(ev.get("timeUnixNano")) or 0,
                            ev.get("name") or "",
                            ev_attrs_json,
                        ),
                    )
    return inserted


def insert_log_export(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    """Walk ExportLogsServiceRequest. Returns inserted-record count."""
    inserted = 0
    for rl in payload.get("resourceLogs") or []:
        resource_id = _upsert_resource(conn, rl.get("resource"))
        for sl in rl.get("scopeLogs") or []:
            scope = sl.get("scope") or {}
            scope_name = scope.get("name")
            scope_version = scope.get("version")
            for rec in sl.get("logRecords") or []:
                attrs = _kv_array_to_dict(rec.get("attributes") or [])
                attrs_json = (
                    json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)
                    if attrs else None
                )
                body = rec.get("body") or {}
                body_text = _attr_value(body) if body else None
                if body_text is not None and not isinstance(body_text, str):
                    body_text = json.dumps(body_text, separators=(",", ":"), ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO dash0_log_records(
                        trace_id, span_id, conversation_id,
                        time_ns, observed_time_ns,
                        severity_number, severity_text,
                        body_text, attrs_json,
                        resource_id, scope_name, scope_version
                    ) VALUES (?, ?, ?,  ?, ?,  ?, ?,  ?, ?,  ?, ?, ?)
                    """,
                    (
                        rec.get("traceId"),
                        rec.get("spanId"),
                        attrs.get("gen_ai.conversation.id"),
                        _coerce_ns(rec.get("timeUnixNano")),
                        _coerce_ns(rec.get("observedTimeUnixNano")),
                        rec.get("severityNumber"),
                        rec.get("severityText"),
                        body_text,
                        attrs_json,
                        resource_id, scope_name, scope_version,
                    ),
                )
                inserted += 1
    return inserted
