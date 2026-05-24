"""Postgres backend stub.

No real network calls. The point of this module is twofold:

1. Lock in the `Backend` shape so a future port to Vercel Postgres + pgvector
   is a swap, not a refactor of the capture pipeline.
2. Translate the existing SQLite migration corpus to a Postgres-flavoured
   preview so we can eyeball what the DDL *would* look like before we wire
   up a real connection.

Every method except `apply_schema` raises `NotImplementedError`. `apply_schema`
walks the same `migrations/*.{sql,py}` files the SQLite runner uses, applies
naive dialect substitutions, and prints the result to stdout.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Backend

# Migration files live next to the runner.
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Lazy import-time error for psycopg etc. is deferred -- this module is
# intentionally importable without any pg driver installed.


def _mask_dsn(dsn: str) -> str:
    """Hide credentials in a DSN for log/error messages."""
    # postgresql://user:pass@host/db  ->  postgresql://***@host/db
    return re.sub(r"://[^@/]+@", "://***@", dsn)


# ---------------------------------------------------------------------------
# SQLite -> Postgres dialect translation
# ---------------------------------------------------------------------------

# Order matters: more-specific patterns first.
_VEC0_RE = re.compile(
    r"CREATE\s+VIRTUAL\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+USING\s+vec0\s*\([^)]*FLOAT\[(\d+)\][^)]*\)\s*;?",
    re.IGNORECASE | re.DOTALL,
)
_FTS5_RE = re.compile(
    r"CREATE\s+VIRTUAL\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+USING\s+fts5\s*\([^;]*\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_AUTOINC_RE = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE)
_DATETIME_NOW_RE = re.compile(r"datetime\('now'\)", re.IGNORECASE)


def _translate_sql(sql: str) -> str:
    """Coarse SQLite -> Postgres translation. Preview-quality, not production."""

    def _vec_repl(match: re.Match) -> str:
        table = match.group(1)
        dim = match.group(2)
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            f"    turn_id    BIGINT PRIMARY KEY,\n"
            f"    embedding  vector({dim})\n"
            f");\n"
            f"-- TODO[pg]: CREATE INDEX ON {table} USING ivfflat (embedding vector_l2_ops);"
        )

    def _fts_repl(match: re.Match) -> str:
        table = match.group(1)
        return (
            f"-- TODO[pg]: FTS5 virtual table `{table}` has no direct equivalent.\n"
            f"-- Replace with a tsvector column on `turns` + GIN index + triggers, e.g.:\n"
            f"--   ALTER TABLE turns ADD COLUMN search_tsv tsvector;\n"
            f"--   CREATE INDEX idx_turns_tsv ON turns USING GIN(search_tsv);"
        )

    out = sql
    out = _VEC0_RE.sub(_vec_repl, out)
    out = _FTS5_RE.sub(_fts_repl, out)
    out = _AUTOINC_RE.sub("BIGSERIAL PRIMARY KEY", out)
    out = _DATETIME_NOW_RE.sub("now()", out)
    return out


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class PostgresBackend(Backend):
    """Stub Postgres backend. `apply_schema` prints a preview; writes raise."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    # ---- helpers ------------------------------------------------------

    def _nyi(self, what: str) -> NotImplementedError:
        return NotImplementedError(
            f"Postgres backend not yet implemented ({what}); DSN={_mask_dsn(self.dsn)}"
        )

    # ---- Backend impl -------------------------------------------------

    def connect(self) -> Any:
        raise self._nyi("connect")

    def apply_schema(self) -> None:
        """Walk migrations/*.{sql,py} in order, translate, and print.

        Does NOT execute anything. Does NOT raise. Returns None.
        """
        print(f"# Postgres dry-run schema preview")
        print(f"# DSN: {_mask_dsn(self.dsn)}")
        print(f"# Source: {_MIGRATIONS_DIR}")
        print()

        files = sorted(
            p for p in _MIGRATIONS_DIR.iterdir()
            if re.match(r"^\d+_[\w\-]+\.(sql|py)$", p.name)
        )
        for path in files:
            print(f"-- ---------- {path.name} ----------")
            if path.suffix == ".sql":
                raw = path.read_text(encoding="utf-8")
                print(_translate_sql(raw).rstrip())
            else:
                # .py migrations execute Python against a live connection;
                # there is no SQL string to translate. Flag for follow-up.
                print(
                    f"-- TODO[pg]: {path.name} is a Python migration; reimplement against "
                    f"psycopg/asyncpg when wiring up the real PostgresBackend."
                )
            print()
        return None

    def upsert_session(self, turn) -> None:
        raise self._nyi("upsert_session")

    def insert_turn(
        self,
        turn,
        embedding,
        *,
        origin: str = "organic",
        prompt_embedding=None,
    ) -> int | None:
        raise self._nyi("insert_turn")

    def query_turns(self, where_clause: str, params: list) -> list[dict]:
        raise self._nyi("query_turns")
