-- Track which backend tier served each AI call so the dashboard can render
-- the full attempt chain (rocco → ollama → anthropic) and so the daily $ cap
-- can filter on `backend='anthropic'` — local rows shouldn't count toward the
-- spend cap because they cost ~$0.
ALTER TABLE ai_runs ADD COLUMN backend TEXT DEFAULT 'anthropic';

CREATE INDEX IF NOT EXISTS idx_ai_runs_backend ON ai_runs(backend);
