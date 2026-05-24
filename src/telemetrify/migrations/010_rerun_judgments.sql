-- LLM verdict on each rerun: better / same / worse / inconclusive vs original.
-- Per-dimension scores live in dimensions_json (zstd JSON blob).
CREATE TABLE IF NOT EXISTS rerun_judgments (
    rerun_id          INTEGER PRIMARY KEY REFERENCES reruns(id) ON DELETE CASCADE,
    verdict           TEXT NOT NULL,            -- 'better' | 'same' | 'worse' | 'inconclusive'
    confidence        REAL,                     -- 0.0 - 1.0
    reasoning         TEXT,                     -- ≤ 40 words
    dimensions_json   BLOB,                     -- zstd JSON: {a:{q,c,a}, b:{q,c,a}}
    model             TEXT,                     -- judge model id
    prompt_version    TEXT,
    generated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    cost_usd          REAL
);

CREATE INDEX IF NOT EXISTS idx_rerun_judgments_verdict ON rerun_judgments(verdict);
