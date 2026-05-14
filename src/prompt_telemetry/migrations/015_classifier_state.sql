-- Local grader classifier: persistent model registry + per-turn predictions.
-- Trained from `auto_grades` (silver labels, weight=1) and `annotations`
-- (gold labels, weight=3). Inference replaces the LLM grader for high-volume
-- scoring when cost matters.

CREATE TABLE IF NOT EXISTS classifier_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL,
    trained_at      TEXT NOT NULL,
    n_train         INTEGER,
    n_val           INTEGER,
    accuracy        REAL,
    f1_macro        REAL,
    features_version TEXT,
    notes           TEXT,
    is_active       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_classifier_models_active ON classifier_models(is_active);

CREATE TABLE IF NOT EXISTS classifier_predictions (
    turn_id      INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    quality      INTEGER,          -- coarse class: 1=low, 3=mid, 5=high
    confidence   REAL,
    model_id     INTEGER REFERENCES classifier_models(id) ON DELETE SET NULL,
    predicted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_classifier_predictions_model ON classifier_predictions(model_id);
