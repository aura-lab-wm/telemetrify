-- Per-turn LLM-as-Judge auto-grades. One row per turn (turn_id as PK).
-- Populated inline by the Stop hook (capture.py) and by bin/grade for backfill.
CREATE TABLE IF NOT EXISTS auto_grades (
    turn_id           INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    quality           INTEGER,         -- 1-5 overall
    hallucination     TEXT,            -- 'low' | 'med' | 'high'
    completeness      INTEGER,         -- 1-5
    refusal           INTEGER,         -- 0/1 boolean
    followed_request  INTEGER,         -- 1-5
    notes             TEXT,            -- ≤ 20 words
    model             TEXT,            -- grader model id (e.g. claude-haiku-4-5-...)
    prompt_version    TEXT,            -- "grader-v1" etc; lets us re-grade after prompt edits
    generated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    cost_usd          REAL,            -- per-call cost as recorded by ai_runs
    raw_json          BLOB             -- zstd-compressed full LLM response
);

CREATE INDEX IF NOT EXISTS idx_auto_grades_quality       ON auto_grades(quality);
CREATE INDEX IF NOT EXISTS idx_auto_grades_hallucination ON auto_grades(hallucination);
CREATE INDEX IF NOT EXISTS idx_auto_grades_generated     ON auto_grades(generated_at);
