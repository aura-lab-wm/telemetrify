-- 016_dash0_otel.sql
-- Local OTLP/HTTP ingest from dash0-agent-plugin.
-- See deploy/dash0-integration.md for the operator-side runbook.
--
-- Additive only: no ALTER on existing tables. Safe on the live DB.
-- Join hook: dash0_spans.conversation_id == sessions.id (Claude session UUID).

CREATE TABLE IF NOT EXISTS dash0_resources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL UNIQUE,
    attrs_json    TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dash0_spans (
    trace_id        TEXT NOT NULL,
    span_id         TEXT NOT NULL,
    parent_span_id  TEXT,
    conversation_id TEXT,
    name            TEXT NOT NULL,
    kind            INTEGER,
    start_ns        INTEGER NOT NULL,
    end_ns          INTEGER,
    status_code     INTEGER,
    status_message  TEXT,
    attrs_json      TEXT,
    resource_id     INTEGER REFERENCES dash0_resources(id),
    scope_name      TEXT,
    scope_version   TEXT,
    received_at     TEXT NOT NULL DEFAULT (datetime('now')),
    raw_z           BLOB,
    PRIMARY KEY (trace_id, span_id)
);

CREATE INDEX IF NOT EXISTS idx_dash0_spans_conversation ON dash0_spans(conversation_id, start_ns);
CREATE INDEX IF NOT EXISTS idx_dash0_spans_trace        ON dash0_spans(trace_id, start_ns);
CREATE INDEX IF NOT EXISTS idx_dash0_spans_name         ON dash0_spans(name);
CREATE INDEX IF NOT EXISTS idx_dash0_spans_received     ON dash0_spans(received_at);

CREATE TABLE IF NOT EXISTS dash0_span_events (
    trace_id    TEXT NOT NULL,
    span_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    time_ns     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    attrs_json  TEXT,
    PRIMARY KEY (trace_id, span_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_dash0_span_events_span ON dash0_span_events(trace_id, span_id);

CREATE TABLE IF NOT EXISTS dash0_log_records (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id         TEXT,
    span_id          TEXT,
    conversation_id  TEXT,
    time_ns          INTEGER,
    observed_time_ns INTEGER,
    severity_number  INTEGER,
    severity_text    TEXT,
    body_text        TEXT,
    attrs_json       TEXT,
    resource_id      INTEGER REFERENCES dash0_resources(id),
    scope_name       TEXT,
    scope_version    TEXT,
    received_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dash0_log_records_conversation ON dash0_log_records(conversation_id, time_ns);
CREATE INDEX IF NOT EXISTS idx_dash0_log_records_span         ON dash0_log_records(trace_id, span_id);
CREATE INDEX IF NOT EXISTS idx_dash0_log_records_received     ON dash0_log_records(received_at);
