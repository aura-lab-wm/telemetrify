"""Compress the raw_json TEXT column into raw_json_z BLOB (zstd level=3).

This migration is destructive on the `raw_json` column. The migrations runner
will copy the DB to `data/backups/<ts>-007_raw_json_zstd.db` before applying
because BACKUP_FIRST is set.
"""
import sqlite3

BACKUP_FIRST = True


def up(conn: sqlite3.Connection) -> None:
    import zstandard as zstd

    # 1. Add the new column.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(turns)").fetchall()}
    if "raw_json_z" not in cols:
        conn.execute("ALTER TABLE turns ADD COLUMN raw_json_z BLOB")

    # 2. Compress every existing raw_json TEXT row in batches.
    cctx = zstd.ZstdCompressor(level=3)
    batch_size = 500
    while True:
        rows = conn.execute(
            """
            SELECT id, raw_json FROM turns
            WHERE raw_json IS NOT NULL AND raw_json_z IS NULL
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        if not rows:
            break
        updates = [
            (cctx.compress(r["raw_json"].encode("utf-8")), r["id"])
            for r in rows
        ]
        conn.executemany("UPDATE turns SET raw_json_z = ? WHERE id = ?", updates)

    # 3. Drop the legacy column. SQLite 3.35+ supports DROP COLUMN; we verified 3.51.
    if "raw_json" in cols:
        conn.execute("ALTER TABLE turns DROP COLUMN raw_json")

    # 4. Reclaim space.
    # VACUUM cannot run inside a transaction; the migrations runner handles that
    # by issuing it post-commit when POST_VACUUM is set.


POST_VACUUM = True
