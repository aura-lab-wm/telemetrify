"""Abstract Backend interface.

Phase 8 scaffold. Concrete backends live alongside this module:
- `SqliteBackend` (production today)
- `PostgresBackend` (stub; preview-only schema translation)

The intent is that any future migration to a cloud-hosted Postgres + pgvector
store is purely an implementation swap in this directory, not a refactor of
`capture.py` / `store.py` / `search.py`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Backend(ABC):
    """Storage backend for prompt telemetry."""

    @abstractmethod
    def connect(self) -> Any:
        """Open and return a live connection / handle to the backing store."""
        raise NotImplementedError

    @abstractmethod
    def apply_schema(self) -> None:
        """Apply all known migrations to the backing store."""
        raise NotImplementedError

    @abstractmethod
    def upsert_session(self, turn) -> None:
        """Insert or update the session row for the given Turn."""
        raise NotImplementedError

    @abstractmethod
    def insert_turn(
        self,
        turn,
        embedding,
        *,
        origin: str = "organic",
        prompt_embedding=None,
    ) -> int | None:
        """Insert a turn (plus tool_calls / vectors). Return turn_id or None on conflict."""
        raise NotImplementedError

    @abstractmethod
    def query_turns(self, where_clause: str, params: list) -> list[dict]:
        """Run a parameterised SELECT against `turns` and return rows as dicts."""
        raise NotImplementedError
