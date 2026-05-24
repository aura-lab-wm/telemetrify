-- Audit trail for every AI/LLM call. Used by the budget guard (sum cost_usd
-- where date(started_at)=today) to decide whether the next call is in-budget.
CREATE TABLE IF NOT EXISTS ai_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feature         TEXT NOT NULL,            -- 'grader' | 'cluster_label' | 'rerun_judge' | 'qa' | 'queue' | 'digest' | 'annotate' | 'diet'
    model           TEXT NOT NULL,
    prompt_version  TEXT,
    target_id       TEXT,                     -- e.g. turn_id, cluster_id, rerun_id as text
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        REAL DEFAULT 0,
    status          TEXT NOT NULL,            -- 'pending' | 'success' | 'failure' | 'over_budget' | 'timeout'
    error           TEXT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    duration_ms     INTEGER,
    override_budget INTEGER DEFAULT 0         -- 1 if a --backfill-budget override was in effect
);

CREATE INDEX IF NOT EXISTS idx_ai_runs_feature   ON ai_runs(feature);
CREATE INDEX IF NOT EXISTS idx_ai_runs_started   ON ai_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_ai_runs_status    ON ai_runs(status);
