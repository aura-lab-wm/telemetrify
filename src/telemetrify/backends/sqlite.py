"""SQLite backend — thin adapter around the existing `db` / `store` modules.

This is the production backend today; we keep the existing module-level
functions in `db.py` / `store.py` untouched and merely wrap them so the
rest of the codebase can be migrated to the `Backend` protocol incrementally.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import db as _db
from .. import store as _store
from .base import Backend


class SqliteBackend(Backend):
    def __init__(self, path=None):
        self._path = path  # None -> default DB_PATH inside _db.connect
        self._conn: sqlite3.Connection | None = None

    # ---- connection ---------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _db.connect() if self._path is None else _db.connect(self._path)
        return self._conn

    def apply_schema(self) -> None:
        """Apply all pending migrations via the existing runner.

        `_db.connect()` already invokes `migrations.apply()`, so simply
        opening a connection is sufficient. We do it explicitly here so the
        intent reads clearly at call sites.
        """
        from ..migrations import apply as _apply
        conn = self.connect()
        _apply(conn, log=lambda _msg: None)

    # ---- writes -------------------------------------------------------

    def upsert_session(self, turn) -> None:
        _store.upsert_session(self.connect(), turn)

    def insert_turn(
        self,
        turn,
        embedding,
        *,
        origin: str = "organic",
        prompt_embedding=None,
    ) -> int | None:
        return _store.insert_turn(
            self.connect(),
            turn,
            embedding,
            origin=origin,
            prompt_embedding=prompt_embedding,
        )

    # ---- reads --------------------------------------------------------

    def query_turns(self, filters=None) -> list[dict]:
        """Run a parameterised SELECT against `turns`, scoped by a `Filters`
        fragment.

        This previously took a raw `where_clause: str` spliced directly into
        the SQL text -- a SQL-injection shape (nothing stopped a future
        caller from building that string out of unsanitised input). It now
        takes a `telemetrify.search.Filters` instance instead, exactly the
        mechanism `search.parse_filters()` / `export._turn_query()` already
        use: `.where` is only ever assembled from hardcoded literal clause
        templates (see `parse_filters`), and every actual value is bound via
        `.params`, never interpolated into the SQL string itself.
        """
        from ..search import Filters
        filters = filters or Filters()
        # Alias `t`, matching `parse_filters()`'s clause templates (e.g.
        # `t.model = ?`, `EXISTS (... t.id ...)`) -- those clauses assume this
        # exact alias, same as `search.recent()` / `export._turn_query()`.
        sql = "SELECT t.* FROM turns t"
        if filters.where:
            sql += f" WHERE {filters.where}"
        cur = self.connect().execute(sql, filters.params)
        return [dict(row) for row in cur.fetchall()]
