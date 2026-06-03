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
        self.last_timeout = None

    def is_available(self) -> bool:
        return self._available

    def complete(self, *, system: str, user: str, model: str,
                 max_tokens: int, json_schema: Any | None,
                 timeout: float | None = None):
        self.calls += 1
        self.last_timeout = timeout
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


def test_bad_json_falls_through_to_next_backend(db):
    """A schema call whose first backend returns prose (not JSON) must fall
    through to a stronger tier instead of hard-failing. Regression for the
    bug that broke /ask + the bulk graders the moment local Ollama came up."""
    localmac = FakeBackend("localmac", available=True, text="here is some prose, not json")
    anthropic = FakeBackend("anthropic", available=True, text='{"ok": 1}')
    router = _setup_router(None, db, [localmac, anthropic],
                           order=["localmac", "anthropic"])

    res = router.call(
        feature="grader", template=_make_template(),
        user_kwargs={"who": "x"}, schema={}, max_tokens=64,
    )
    assert res.raw_text == '{"ok": 1}'
    assert localmac.calls == 1 and anthropic.calls == 1
    # one failure row (localmac bad JSON) + one success row (anthropic)
    rows = db.execute("SELECT backend, status FROM ai_runs ORDER BY id").fetchall()
    assert rows[0]["backend"] == "localmac" and rows[0]["status"] == "failure"
    assert rows[1]["backend"] == "anthropic" and rows[1]["status"] == "success"


def test_all_backends_bad_json_raises_after_exhausting(db):
    """If EVERY tier returns invalid JSON for a schema call, the router raises
    after trying them all (no silent empty result)."""
    a = FakeBackend("localmac", available=True, text="prose A")
    b = FakeBackend("anthropic", available=True, text="prose B")
    router = _setup_router(None, db, [a, b], order=["localmac", "anthropic"])

    with pytest.raises(RuntimeError):
        router.call(feature="grader", template=_make_template(),
                    user_kwargs={"who": "x"}, schema={}, max_tokens=64)
    assert a.calls == 1 and b.calls == 1


def test_timeout_is_forwarded_to_backend(db):
    """router.call(timeout=...) must reach backend.complete — previously the
    param was accepted and silently dropped, so the inline grader could block
    for minutes."""
    rocco = FakeBackend("rocco", available=True)
    router = _setup_router(None, db, [rocco])
    router.call(feature="qa", template=_make_template(),
                user_kwargs={"who": "x"}, schema=None, max_tokens=64, timeout=12.5)
    assert rocco.last_timeout == 12.5


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


# ---------------------------------------------------------------------------
# 429 / rate-limit should fall through to the next backend
# (regression for the OAuth-bucket exhaustion that bit /ask in production)
# ---------------------------------------------------------------------------

class _StubResponse:
    def __init__(self, status_code: int): self.status_code = status_code


def test_429_is_transient_and_falls_through():
    """HTTP 429 from one backend must propagate the request to the next.
    Previously 4xx was treated as fatal across the board, which meant a
    rate-limited Anthropic call would never get retried even though Rocco
    or Ollama might have had budget."""
    from telemetrify.ai.router import BackendRouter
    import httpx
    # 429 wrapped in httpx.HTTPStatusError
    exc = httpx.HTTPStatusError(
        "Too Many Requests",
        request=httpx.Request("POST", "https://example.test/"),
        response=httpx.Response(status_code=429))
    assert BackendRouter._is_transient(exc) is True, \
        "429 must be classified transient so the router falls through"


def test_503_is_transient():
    from telemetrify.ai.router import BackendRouter
    import httpx
    exc = httpx.HTTPStatusError(
        "Service Unavailable",
        request=httpx.Request("POST", "https://example.test/"),
        response=httpx.Response(status_code=503))
    assert BackendRouter._is_transient(exc) is True


def test_400_remains_fatal():
    """A 400 Bad Request is a deterministic shape problem — falling
    through to other backends just wastes their quota on the same
    broken payload."""
    from telemetrify.ai.router import BackendRouter
    import httpx
    exc = httpx.HTTPStatusError(
        "Bad Request",
        request=httpx.Request("POST", "https://example.test/"),
        response=httpx.Response(status_code=400))
    assert BackendRouter._is_transient(exc) is False


# ---------------------------------------------------------------------------
# 4-tier router with localmac (Ollama on this Mac)
# ---------------------------------------------------------------------------

def test_default_router_includes_localmac_tier(tmp_path):
    """Regression: when Rocco is offline AND the user's Anthropic OAuth
    bucket is exhausted, /ask should still have a Mac-local fallback.
    The default router builds 4 tiers in order: rocco, localmac, ollama,
    anthropic. Localmac probes localhost:11434/v1 → reports unavailable
    if Ollama isn't installed locally (silent fall-through), so the new
    tier is harmless when absent."""
    import sqlite3
    from telemetrify.ai.router import default_router, _DEFAULT_ORDER
    assert "localmac" in _DEFAULT_ORDER
    assert _DEFAULT_ORDER.index("localmac") == 1, \
        "localmac belongs RIGHT AFTER rocco — free + local + private"

    conn = sqlite3.connect(":memory:")
    router = default_router(conn)
    names = list(router._by_name.keys())
    assert names == ["rocco", "localmac", "ollama", "anthropic", "claude_cli"], \
        f"unexpected backend order: {names}"
    # claude_cli is registered (so a per-feature order can name it) but stays
    # OUT of the default order — bulk features must not reach for the slow tier.
    assert "claude_cli" not in _DEFAULT_ORDER


def test_backend_transient_falls_through(db):
    """A backend that raises BackendTransient (e.g. claude-cli: binary missing,
    non-zero exit, timeout) must fall through to the next tier — same as a
    ConnectError. Regression guard for the /ask fix."""
    from telemetrify.ai.backends.base import BackendTransient

    cli = FakeBackend("claude_cli", available=True,
                      raise_on_call=BackendTransient("claude CLI exit 1"))
    anthropic = FakeBackend("anthropic", available=True, text='{"v": 9}')
    router = _setup_router(None, db, [cli, anthropic],
                           order=["claude_cli", "anthropic"])

    res = router.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
    )
    assert res.raw_text == '{"v": 9}'
    assert cli.calls == 1
    assert anthropic.calls == 1


def test_explicit_order_param_overrides_feature_env(monkeypatch, db):
    """A per-call `order=` pins the tier sequence even when the feature's env
    default says otherwise — this is how the /ask PLANNER reaches claude_cli
    while the SYNTHESIZER (same feature) keeps the fast default order."""
    localmac = FakeBackend("localmac", available=True, text="prose, not json")
    cli = FakeBackend("claude_cli", available=True, text='{"semantic_query": "ok"}')
    anthropic = FakeBackend("anthropic", available=True)

    # Feature default would pick localmac first…
    monkeypatch.setenv("TELEMETRIFY_LLM_ORDER__qa", "localmac,anthropic")
    router = _setup_router(None, db, [localmac, cli, anthropic],
                           order=["localmac", "anthropic"])

    # …but the explicit order wins for this call.
    res = router.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
        order=["claude_cli", "anthropic"],
    )
    assert res.raw_text == '{"semantic_query": "ok"}'
    assert localmac.calls == 0
    assert cli.calls == 1
    assert anthropic.calls == 0


def test_qa_order_routes_claude_cli_before_localmac(monkeypatch, db):
    """With TELEMETRIFY_LLM_ORDER__qa=claude_cli,anthropic the bad-JSON
    localmac tier is never consulted for /ask — the planner gets reliable
    JSON from the claude-cli tier. This is the production fix."""
    from telemetrify.ai import router as router_mod

    localmac = FakeBackend("localmac", available=True, text="here's some prose, no json")
    cli = FakeBackend("claude_cli", available=True, text='{"semantic_query": "ok"}')
    anthropic = FakeBackend("anthropic", available=True)

    monkeypatch.setenv("TELEMETRIFY_LLM_ORDER__qa", "claude_cli,anthropic")

    def fake_default_router(conn, override_budget_usd=None):
        return router_mod.BackendRouter(
            conn=conn, backends=[localmac, cli, anthropic],
            default_order=["localmac", "claude_cli", "anthropic"],
            override_budget_usd=override_budget_usd,
        )
    monkeypatch.setattr(router_mod, "default_router", fake_default_router)

    r = router_mod.default_router(db)
    res = r.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
    )
    assert res.raw_text == '{"semantic_query": "ok"}'
    assert localmac.calls == 0, "localmac must be skipped for qa"
    assert cli.calls == 1
    assert anthropic.calls == 0
