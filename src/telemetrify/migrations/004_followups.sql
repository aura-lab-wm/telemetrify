-- Within-session retry/correction detection.
CREATE TABLE IF NOT EXISTS turn_followups (
    turn_id       INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    prev_turn_id  INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,         -- 'paraphrase' | 'corrective' | 'both'
    reason        TEXT,                  -- short explanation (regex match / distance value)
    distance      REAL,                  -- prompt-embedding distance to prev turn, when available
    detected_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_followups_prev ON turn_followups(prev_turn_id);
CREATE INDEX IF NOT EXISTS idx_followups_kind ON turn_followups(kind);
