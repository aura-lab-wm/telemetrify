-- Manual quality annotations on turns. One row per turn (turn_id is PK).
CREATE TABLE IF NOT EXISTS annotations (
    turn_id            INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    rating             INTEGER NOT NULL DEFAULT 0,        -- -1 / 0 / +1
    label              TEXT,                              -- good|bad|regression|expected|...
    tags               TEXT,                              -- CSV
    expected_behavior  TEXT,
    notes              TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_annotations_rating ON annotations(rating);
CREATE INDEX IF NOT EXISTS idx_annotations_label  ON annotations(label);
