"""LLMBackend protocol + shared types.

Every backend exposes:
  - `name`: short label persisted in `ai_runs.backend`
  - `is_available()`: cheap probe (cached ~30s) — does the router even try us?
  - `complete(system, user, model, max_tokens, json_schema)` → `BackendResponse`

Schema-validation + retry live in the router, not here, so this stays small.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class BackendResponse:
    raw_text: str
    input_tokens: int
    output_tokens: int
    model: str


class BackendUnavailable(RuntimeError):
    """is_available() returned False / probe failed deterministically."""


class BackendTransient(RuntimeError):
    """Transient transport error — router should fall through to the next tier."""


@runtime_checkable
class LLMBackend(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        json_schema: Any | None,
        timeout: float | None = None,
    ) -> BackendResponse: ...
