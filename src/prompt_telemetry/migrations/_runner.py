"""Versioned schema migrations.

Migration files live in `prompt_telemetry/migrations/NNN_name.{sql,py}` and are
applied in numeric order. The ledger is the `schema_version` table:

    CREATE TABLE schema_version (
        version    INTEGER PRIMARY KEY,
        name       TEXT,
        applied_at TEXT NOT NULL
    );

Each migration file:
- `.sql` — executed via executescript(); pure DDL/DML.
- `.py`  — imported; must expose `def up(conn): ...`.
           Optional module-level booleans:
             BACKUP_FIRST = True  → copy DB file before running
             POST_VACUUM  = True  → run VACUUM after committing
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import DATA_DIR, DB_PATH

MIGRATIONS_DIR = Path(__file__).resolve().parent
BACKUPS_DIR = DATA_DIR / "backups"

LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT,
    applied_at TEXT NOT NULL
);
"""

_FILE_RE = re.compile(r"^(\d+)_([\w\-]+)\.(sql|py)$")


def _discover() -> list[tuple[int, str, Path]]:
    found: list[tuple[int, str, Path]] = []
    for p in MIGRATIONS_DIR.iterdir():
        m = _FILE_RE.match(p.name)
        if not m:
            continue
        found.append((int(m.group(1)), m.group(2), p))
    found.sort(key=lambda t: t[0])
    return found


def _applied(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {row[0] for row in rows}


def _load_py(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


def _backup_db(tag: str) -> Path | None:
    if not DB_PATH.exists():
        return None
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUPS_DIR / f"{ts}-{tag}.db"
    shutil.copy2(DB_PATH, dest)
    return dest


def _apply_one(conn: sqlite3.Connection, version: int, name: str, path: Path,
               log: Callable[[str], None]) -> None:
    log(f"  applying {version:03d}_{name} ({path.suffix})")
    needs_vacuum = False

    if path.suffix == ".sql":
        with path.open("r", encoding="utf-8") as f:
            sql = f.read()
        conn.executescript(sql)
    else:
        mod = _load_py(path)
        if getattr(mod, "BACKUP_FIRST", False):
            backup = _backup_db(f"{version:03d}_{name}")
            if backup:
                log(f"    backed up to {backup.relative_to(DATA_DIR)}")
        mod.up(conn)
        needs_vacuum = bool(getattr(mod, "POST_VACUUM", False))

    conn.execute(
        "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()

    if needs_vacuum:
        log("    running VACUUM")
        conn.execute("VACUUM")


def _with_lock(fn):
    """Serialize migration application across processes via fcntl.flock."""
    def wrapper(*args, **kwargs):
        import fcntl
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = DATA_DIR / ".migrations.lock"
        with lock_path.open("w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                return fn(*args, **kwargs)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)
    return wrapper


@_with_lock
def apply(conn: sqlite3.Connection, log: Callable[[str], None] = print) -> int:
    """Apply pending migrations. Returns count applied. Process-safe (fcntl lock)."""
    conn.executescript(LEDGER_SQL)
    # backfill `name` column for old schema_version rows from v1 (which only had `version`)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(schema_version)").fetchall()}
    if "name" not in cols:
        conn.execute("ALTER TABLE schema_version ADD COLUMN name TEXT")
        conn.commit()

    applied = _applied(conn)
    pending = [(v, n, p) for v, n, p in _discover() if v not in applied]
    if not pending:
        return 0
    log(f"applying {len(pending)} migration(s)…")
    for version, name, path in pending:
        try:
            _apply_one(conn, version, name, path, log)
        except Exception:
            conn.rollback()
            raise
    return len(pending)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m prompt_telemetry.migrations apply` / `status`."""
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"

    import sqlite_vec
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    if cmd == "status":
        conn.executescript(LEDGER_SQL)
        applied = _applied(conn)
        all_migs = _discover()
        for v, n, p in all_migs:
            mark = "✓" if v in applied else " "
            print(f"  [{mark}] {v:03d}_{n}.{p.suffix.lstrip('.')}")
        print(f"\n{len(applied)}/{len(all_migs)} applied")
        return 0
    elif cmd == "apply":
        n = apply(conn)
        print(f"\napplied {n} migration(s).")
        return 0
    else:
        print(f"unknown command: {cmd}\nusage: python -m prompt_telemetry.migrations [apply|status]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
