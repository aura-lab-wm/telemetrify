-- Full-fidelity replay-and-diff infrastructure.
ALTER TABLE turns ADD COLUMN origin TEXT NOT NULL DEFAULT 'organic';
CREATE INDEX IF NOT EXISTS idx_turns_origin ON turns(origin);

CREATE TABLE IF NOT EXISTS reruns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    original_turn_id    INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    replay_turn_id      INTEGER REFERENCES turns(id) ON DELETE SET NULL,  -- null until the Stop hook ingests the rerun's transcript
    replay_session_id   TEXT,
    model               TEXT,
    total_cost_usd      REAL,
    num_turns           INTEGER,
    duration_ms         INTEGER,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    response_text       TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',   -- pending|success|failure|over_budget
    error_message       TEXT,
    workspace_path      TEXT,
    run_at              TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_reruns_orig   ON reruns(original_turn_id);
CREATE INDEX IF NOT EXISTS idx_reruns_status ON reruns(status);
