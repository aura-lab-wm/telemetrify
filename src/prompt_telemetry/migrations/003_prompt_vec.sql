-- Prompt-only embeddings (no response prefix) for paraphrase detection + clustering.
CREATE VIRTUAL TABLE IF NOT EXISTS prompt_vec USING vec0(
    turn_id  INTEGER PRIMARY KEY,
    embedding FLOAT[384]
);

CREATE TABLE IF NOT EXISTS prompt_clusters (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    label                 TEXT,
    representative_turn_id INTEGER REFERENCES turns(id) ON DELETE SET NULL,
    member_count          INTEGER DEFAULT 0,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS turn_cluster (
    turn_id                INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    cluster_id             INTEGER REFERENCES prompt_clusters(id) ON DELETE SET NULL,
    similarity_to_centroid REAL,
    assigned_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_turn_cluster_cluster ON turn_cluster(cluster_id);
