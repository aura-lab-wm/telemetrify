"""Unit tests for BackendRouter.

Strategy: build a list of `FakeBackend` instances with knobs for
availability, raise-on-call, and a canned BackendResponse. Assert:
  (a) first-available is picked
  (b) ConnectError on first backend falls through to the next
  (c) BudgetExceeded does NOT fall through (raised immediately)
  (d) one `ai_runs` row per *attempted* backend with correct status + backend col
  (e) per-feature env override pins backend
"""
from __future__ import annotations

import sqlite3
from typing import Any

import httpx
import pytest


# ── helper fake backend ─────────────────────────────────────────────────
class FakeBackend:
    def __init__(self, name: str, *, available: bool = True,
                 raise_on_call: Exception | None = None,
                 text: str = '{"ok": 1}', in_tok: int = 5, out_tok: int = 7,
                 in_price: float = 0.0, out_price: float = 0.0):
        self.name = name
        self._available = available
        self._raise = raise_on_call
        self._text = text
        self._in_tok = in_tok
        self._out_tok = out_tok
        self._in_price = in_price
        self._out_price = out_price
        self.calls = 0

    def is_available(self) -> bool:
        return self._available

    def complete(self, *, system: str, user: str, model: str,
                 max_tokens: int, json_schema: Any | None):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        from telemetrify.ai.backends.base import BackendResponse
        return BackendResponse(
            raw_text=self._text, input_tokens=self._in_tok,
            output_tokens=self._out_tok, model=model,
        )

    def estimate_cost(self, in_tok: int, out_tok: int) -> float:
        return (in_tok * self._in_price + out_tok * self._out_price) / 1_000_000.0


def _make_template():
    """Return a minimal stand-in for prompts.PromptTemplate."""
    from telemetrify.ai import prompts as P
    return P.PromptTemplate(
        version="test-v1", model="test-model",
        system="be terse", user_template="hello {who}",
    )


def _setup_router(monkeypatch, conn, backends, *, order=None):
    """Build a BackendRouter with explicit backends + order."""
    from telemetrify.ai.router import BackendRouter
    if order is None:
        order = [b.name for b in backends]
    router = BackendRouter(conn=conn, backends=backends, default_order=order)
    return router


# ── fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture
def db(migrated_db):
    return migrated_db


# ── tests ────────────────────────────────────────────────────────────────
def test_first_available_is_picked(db):
    """If rocco is up, it's used and ollama+anthropic are never called."""
    rocco = FakeBackend("rocco", available=True)
    ollama = FakeBackend("ollama", available=True)
    anthropic = FakeBackend("anthropic", available=True)
    router = _setup_router(None, db, [rocco, ollama, anthropic])

    res = router.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "world"}, schema=None, max_tokens=64,
    )
    assert res.raw_text == '{"ok": 1}'
    assert rocco.calls == 1
    assert ollama.calls == 0
    assert anthropic.calls == 0


def test_connect_error_falls_through(db):
    """ConnectError on rocco → router moves to ollama."""
    rocco = FakeBackend("rocco", available=True,
                        raise_on_call=httpx.ConnectError("tunnel down"))
    ollama = FakeBackend("ollama", available=True, text='{"v": 2}')
    anthropic = FakeBackend("anthropic", available=True)
    router = _setup_router(None, db, [rocco, ollama, anthropic])

    res = router.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
    )
    assert res.raw_text == '{"v": 2}'
    assert rocco.calls == 1
    assert ollama.calls == 1
    assert anthropic.calls == 0


def test_budget_exceeded_does_not_fall_through(db):
    """BudgetExceeded is a fatal deterministic signal; do NOT try the next backend."""
    from telemetrify.ai.client import BudgetExceeded

    rocco = FakeBackend("rocco", available=True,
                        raise_on_call=BudgetExceeded("cap"))
    ollama = FakeBackend("ollama", available=True)
    router = _setup_router(None, db, [rocco, ollama])

    with pytest.raises(BudgetExceeded):
        router.call(
            feature="qa", template=_make_template(),
            user_kwargs={"who": "x"}, schema=None, max_tokens=64,
        )
    assert rocco.calls == 1
    assert ollama.calls == 0


def test_one_ai_runs_row_per_attempted_backend(db):
    """When rocco fails → ollama succeeds, we want two rows:
    one rocco/failure, one ollama/success, both with the new `backend` column."""
    rocco = FakeBackend("rocco", available=True,
                        raise_on_call=httpx.ConnectError("down"))
    ollama = FakeBackend("ollama", available=True, text='{"hi": 1}')
    router = _setup_router(None, db, [rocco, ollama])

    router.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
    )

    rows = db.execute(
        "SELECT backend, status FROM ai_runs ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["backend"] == "rocco"
    assert rows[0]["status"] == "failure"
    assert rows[1]["backend"] == "ollama"
    assert rows[1]["status"] == "success"


def test_per_feature_env_override_pins_backend(monkeypatch, db):
    """`TELEMETRIFY_LLM_ORDER__grader=anthropic` → grader skips rocco/ollama."""
    from telemetrify.ai import router as router_mod

    rocco = FakeBackend("rocco", available=True)
    ollama = FakeBackend("ollama", available=True)
    anthropic = FakeBackend("anthropic", available=True, text='{"a": 1}')

    monkeypatch.setenv("TELEMETRIFY_LLM_ORDER", "rocco,ollama,anthropic")
    monkeypatch.setenv("TELEMETRIFY_LLM_ORDER__grader", "anthropic")

    # Patch default_router so we control which backends are used; the
    # function should respect per-feature env override when building order.
    def fake_default_router(conn, override_budget_usd=None):
        return router_mod.BackendRouter(
            conn=conn, backends=[rocco, ollama, anthropic],
            default_order=["rocco", "ollama", "anthropic"],
            override_budget_usd=override_budget_usd,
        )
    monkeypatch.setattr(router_mod, "default_router", fake_default_router)

    r = router_mod.default_router(db)
    res = r.call(
        feature="grader", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
    )
    assert res.raw_text == '{"a": 1}'
    assert rocco.calls == 0
    assert ollama.calls == 0
    assert anthropic.calls == 1
