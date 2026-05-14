-- LLM-generated semantic labels for prompt_clusters. The raw `label` (first
-- line of the representative prompt) stays as a fallback; `auto_label` is the
-- 2-7 word caption rendered in the UI when present.
ALTER TABLE prompt_clusters ADD COLUMN auto_label TEXT;
ALTER TABLE prompt_clusters ADD COLUMN auto_label_at TEXT;
ALTER TABLE prompt_clusters ADD COLUMN auto_label_model TEXT;
ALTER TABLE prompt_clusters ADD COLUMN auto_label_version TEXT;
