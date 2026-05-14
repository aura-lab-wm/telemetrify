-- FTS5 BM25 index over user_text + assistant_text, kept in sync with `turns` via triggers.
-- content='turns' + content_rowid='id' means FTS shares storage with the base table.
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    user_text,
    assistant_text,
    content='turns',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

-- Sync triggers.
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, user_text, assistant_text)
    VALUES (new.id, new.user_text, new.assistant_text);
END;

CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, user_text, assistant_text)
    VALUES ('delete', old.id, old.user_text, old.assistant_text);
END;

CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, user_text, assistant_text)
    VALUES ('delete', old.id, old.user_text, old.assistant_text);
    INSERT INTO turns_fts(rowid, user_text, assistant_text)
    VALUES (new.id, new.user_text, new.assistant_text);
END;

-- Backfill the index for any rows that pre-date these triggers.
INSERT INTO turns_fts(turns_fts) VALUES ('rebuild');
