import sqlite3
import struct
import threading
from pathlib import Path
from typing import Iterable

import sqlite_vec

from . import DB_PATH, DATA_DIR

EMBEDDING_DIM = 384

# Per-thread connection cache. uvicorn serves sync routes from a bounded
# threadpool, so reusing one connection per (thread, path) caps open file
# descriptors at ~threadpool size instead of leaking one per request — which
# is what drove the process past the FD limit into SQLITE_CANTOPEN.
_local = threading.local()


def _raw_connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), timeout=30.0, check_same_thread=check_same_thread
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Return this thread's connection for `path`, opening + migrating on first
    use. The connection is reused across calls on the same thread so we don't
    leak a file descriptor (and re-run migrations) per request.

    A caller that closes the connection is tolerated: the next call detects the
    dead handle and transparently reopens. Each thread gets its own connection,
    so the cache is safe under sqlite3's default check_same_thread."""
    key = str(path)
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "key", None) == key:
        try:
            cached.execute("SELECT 1")  # liveness probe; raises if closed
            return cached
        except sqlite3.Error:
            try:
                cached.close()
            except Exception:
                pass
            _local.conn = None

    conn = _raw_connect(path)
    from . import migrations  # local import avoids package-cycle on first import
    migrations.apply(conn, log=lambda _msg: None)  # quiet by default
    _local.conn = conn
    _local.key = key
    return conn


def connect_uncached(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a brand-new connection that is *not* stored in the per-thread
    cache and is *not* pinned to the calling thread (`check_same_thread=False`).

    Use this ONLY for a caller that will drive the connection strictly
    sequentially (never concurrently) but may do so from a *different* OS
    thread on each access. That pattern is exactly what a sync generator
    handed to Starlette's `StreamingResponse` produces: Starlette iterates it
    via `iterate_in_threadpool`, which dispatches each `next()` call
    independently through anyio's worker thread pool rather than pinning the
    whole generator to one thread. `connect()`'s cached, thread-affine
    connection breaks under that pattern ("SQLite objects created in a
    thread can only be used in that same thread") the moment two consecutive
    `next()` calls land on different worker threads -- which is common on
    any real (non-trivial) streamed result set. A dedicated, uncached,
    `check_same_thread=False` connection sidesteps this because SQLite
    itself has no problem being used from different threads one after
    another; only Python's *default* same-thread check is stricter than the
    underlying library requires.

    The caller owns the full lifecycle: close it explicitly (ideally in a
    `try/finally` around the code that uses it) when done. It is deliberately
    excluded from `_local` so it can never leak into, or be confused with,
    the shared per-thread cache that every other (non-streaming-across-
    threads) route relies on via `connect()`.
    """
    conn = _raw_connect(path, check_same_thread=False)
    from . import migrations  # local import avoids package-cycle on first import
    migrations.apply(conn, log=lambda _msg: None)  # quiet by default; idempotent
    return conn


def serialize_embedding(vec: Iterable[float]) -> bytes:
    floats = list(vec)
    if len(floats) != EMBEDDING_DIM:
        raise ValueError(f"expected {EMBEDDING_DIM}-dim vector, got {len(floats)}")
    return struct.pack(f"{EMBEDDING_DIM}f", *floats)
