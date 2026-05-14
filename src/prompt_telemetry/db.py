import sqlite3
import struct
from pathlib import Path
from typing import Iterable

import sqlite_vec

from . import DB_PATH, DATA_DIR

EMBEDDING_DIM = 384


def _raw_connect(path: Path) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a connection and apply any pending migrations."""
    conn = _raw_connect(path)
    from . import migrations  # local import avoids package-cycle on first import
    migrations.apply(conn, log=lambda _msg: None)  # quiet by default
    return conn


def serialize_embedding(vec: Iterable[float]) -> bytes:
    floats = list(vec)
    if len(floats) != EMBEDDING_DIM:
        raise ValueError(f"expected {EMBEDDING_DIM}-dim vector, got {len(floats)}")
    return struct.pack(f"{EMBEDDING_DIM}f", *floats)
