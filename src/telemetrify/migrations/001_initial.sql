-- v1 baseline schema.
-- Note: this file is applied via executescript() and shares a transaction with
-- the migration ledger insert. Do not include BEGIN/COMMIT.

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    last_turn_at    TEXT,
    cwd             TEXT NOT NULL,
    git_branch      TEXT,
    project_dir     TEXT,
    transcript_path TEXT,
    entrypoint      TEXT,
    user_type       TEXT,
    cli_version     TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id                  TEXT NOT NULL REFERENCES sessions(id),
    prompt_id                   TEXT,
    user_uuid                   TEXT UNIQUE,
    parent_uuid                 TEXT,
    user_text                   TEXT NOT NULL,
    assistant_text              TEXT NOT NULL,
    thinking_text               TEXT,
    model                       TEXT,
    started_at                  TEXT NOT NULL,
    finished_at                 TEXT,
    latency_ms                  INTEGER,
    input_tokens                INTEGER,
    output_tokens               INTEGER,
    cache_creation_tokens       INTEGER,
    cache_read_tokens           INTEGER,
    cwd                         TEXT,
    git_branch                  TEXT,
    cli_version                 TEXT,
    attribution_skill           TEXT,
    attribution_plugin          TEXT,
    tool_call_count             INTEGER DEFAULT 0,
    assistant_message_count     INTEGER DEFAULT 0,
    raw_json                    TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session     ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_started     ON turns(started_at);
CREATE INDEX IF NOT EXISTS idx_turns_model       ON turns(model);
CREATE INDEX IF NOT EXISTS idx_turns_cwd         ON turns(cwd);
CREATE INDEX IF NOT EXISTS idx_turns_skill       ON turns(attribution_skill);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id         INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    tool_use_id     TEXT,
    input_json      TEXT,
    output_text     TEXT,
    is_error        INTEGER DEFAULT 0,
    started_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_turn ON tool_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT NOT NULL,
    inserted    INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    note        TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS turn_vec USING vec0(
    turn_id  INTEGER PRIMARY KEY,
    embedding FLOAT[384]
);
