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
import threading
import time
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


# ---------------------------------------------------------------------------
# BUG 1 — ai_runs.model must reflect the model that ACTUALLY served the call,
# not stay frozen at the originally-requested template model.
# ---------------------------------------------------------------------------

class _ModelSubstitutingBackend(FakeBackend):
    """Mimics a backend (e.g. OpenAICompatBackend) that ignores the
    requested model and serves its own configured one — BackendResponse.model
    is the SERVED model, which may differ from what was asked for."""

    def __init__(self, name: str, served_model: str, **kwargs):
        super().__init__(name, **kwargs)
        self._served_model = served_model

    def complete(self, *, system: str, user: str, model: str,
                 max_tokens: int, json_schema: Any | None,
                 timeout: float | None = None):
        self.calls += 1
        from telemetrify.ai.backends.base import BackendResponse
        return BackendResponse(
            raw_text=self._text, input_tokens=self._in_tok,
            output_tokens=self._out_tok, model=self._served_model,
        )


def test_success_persists_actual_served_model_not_requested_model(db):
    """The template requests model="test-model", but the backend actually
    serves "actually-served-xyz" (e.g. a local tier substituting its own
    configured model). ai_runs.model must record the SERVED model."""
    backend = _ModelSubstitutingBackend(
        "rocco", "actually-served-xyz", available=True)
    router = _setup_router(None, db, [backend])

    res = router.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
    )
    assert res.model == "actually-served-xyz"

    row = db.execute(
        "SELECT model FROM ai_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["model"] == "actually-served-xyz", (
        "ai_runs.model must be corrected to the model that ACTUALLY served "
        "the request, not stay frozen at the originally-requested template "
        "model — every fallback-tier row was mislabeled before this fix"
    )


def test_bad_json_failure_row_also_persists_actual_served_model(db):
    """Even a row that fails schema validation got a REAL response from a
    REAL backend/model — that row's `model` column should reflect the
    served model too, not just success rows."""
    weak = _ModelSubstitutingBackend(
        "localmac", "weak-tier-actual-model", available=True, text="not json")
    anthropic = FakeBackend("anthropic", available=True, text='{"ok": 1}')
    router = _setup_router(None, db, [weak, anthropic],
                           order=["localmac", "anthropic"])

    router.call(feature="grader", template=_make_template(),
               user_kwargs={"who": "x"}, schema={}, max_tokens=64)

    rows = db.execute(
        "SELECT backend, model, status FROM ai_runs ORDER BY id"
    ).fetchall()
    assert rows[0]["backend"] == "localmac"
    assert rows[0]["status"] == "failure"
    assert rows[0]["model"] == "weak-tier-actual-model"


# ---------------------------------------------------------------------------
# BUG 3 — 401/403 (auth failure scoped to ONE tier) must fall through, unlike
# a genuinely malformed request (400), which stays fatal.
# ---------------------------------------------------------------------------

def test_401_is_transient():
    from telemetrify.ai.router import BackendRouter
    exc = httpx.HTTPStatusError(
        "Unauthorized",
        request=httpx.Request("POST", "https://example.test/"),
        response=httpx.Response(status_code=401))
    assert BackendRouter._is_transient(exc) is True


def test_403_is_transient():
    from telemetrify.ai.router import BackendRouter
    exc = httpx.HTTPStatusError(
        "Forbidden",
        request=httpx.Request("POST", "https://example.test/"),
        response=httpx.Response(status_code=403))
    assert BackendRouter._is_transient(exc) is True


def test_404_remains_fatal():
    """Unlike 401/403, other 4xx codes stay fatal — the fix is deliberately
    scoped to auth failures (unambiguously tier-specific), not a blanket
    "all 4xx are actually fine" change."""
    from telemetrify.ai.router import BackendRouter
    exc = httpx.HTTPStatusError(
        "Not Found",
        request=httpx.Request("POST", "https://example.test/"),
        response=httpx.Response(status_code=404))
    assert BackendRouter._is_transient(exc) is False


def test_anthropic_sdk_401_is_transient():
    """Same classification via the anthropic SDK's APIStatusError path (used
    when the anthropic backend itself raises), not just the httpx path the
    OpenAI-compat backends raise through."""
    import anthropic
    from telemetrify.ai.router import BackendRouter
    resp = httpx.Response(
        status_code=401, request=httpx.Request("POST", "https://example.test/"))
    exc = anthropic.APIStatusError("invalid x-api-key", response=resp, body=None)
    assert BackendRouter._is_transient(exc) is True


def test_401_falls_through_to_next_backend_end_to_end(db):
    """End-to-end: a 401 raised from the first backend's complete() must
    fall through to the next tier instead of killing the whole call chain —
    this is what used to make a single rotated/expired key on ONE tier take
    down every tier below it."""
    exc = httpx.HTTPStatusError(
        "Unauthorized",
        request=httpx.Request("POST", "https://example.test/"),
        response=httpx.Response(status_code=401))
    rocco = FakeBackend("rocco", available=True, raise_on_call=exc)
    ollama = FakeBackend("ollama", available=True, text='{"v": 3}')
    router = _setup_router(None, db, [rocco, ollama])

    res = router.call(
        feature="qa", template=_make_template(),
        user_kwargs={"who": "x"}, schema=None, max_tokens=64,
    )
    assert res.raw_text == '{"v": 3}'
    assert rocco.calls == 1
    assert ollama.calls == 1


# ---------------------------------------------------------------------------
# BUG 4 + BUG 6 — a pre-flight budget rejection still writes an ai_runs row,
# and the estimate assumes the full max_tokens ceiling (not max_tokens // 2).
# ---------------------------------------------------------------------------

def test_over_budget_preflight_writes_a_row_using_full_max_tokens_estimate(db, monkeypatch):
    """claude-sonnet-4-6 is $15/M output tokens. At max_tokens=40_000 the
    FULL estimate is $0.60 — over a $0.50 cap. The OLD max_tokens // 2
    estimate would have been $0.30 (under cap) and let this through
    (BUG 6). Either way, the backend must never be called, and a row must
    be written recording the rejection (BUG 4) instead of nothing at all."""
    from telemetrify.ai.client import AnthropicClient, BudgetExceeded

    monkeypatch.setattr(AnthropicClient, "DEFAULT_DAILY_CAP_USD", 0.5)

    anthropic = FakeBackend("anthropic", available=True)
    router = _setup_router(None, db, [anthropic])

    with pytest.raises(BudgetExceeded):
        router.call(
            feature="qa", template=_make_template(),
            user_kwargs={"who": "x"}, schema=None, max_tokens=40_000,
            model_override="claude-sonnet-4-6",
        )
    assert anthropic.calls == 0, "backend must never be called once budget is rejected"

    rows = db.execute(
        "SELECT status, backend, error FROM ai_runs ORDER BY id"
    ).fetchall()
    assert len(rows) == 1, "a rejected pre-flight budget check must still write a row"
    assert rows[0]["backend"] == "anthropic"
    assert rows[0]["status"] == "over_budget"
    assert rows[0]["error"]


# ---------------------------------------------------------------------------
# BUG 2 — the budget check-then-act must not be a TOCTOU race across
# concurrent Anthropic calls (separate connections, mirroring production
# where each request/thread gets its own sqlite connection to the same file).
# ---------------------------------------------------------------------------

class _SlowAnthropicBackend:
    """Simulates a real backend call taking noticeable wall-clock time —
    long enough that, under the OLD buggy flow (check budget, THEN insert a
    cost_usd=0 pending row, THEN call complete(), THEN record real cost),
    two concurrent calls would both clear the budget check while neither's
    real cost had landed yet."""
    name = "anthropic"

    def __init__(self):
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def complete(self, *, system, user, model, max_tokens, json_schema, timeout=None):
        self.calls += 1
        time.sleep(0.15)
        from telemetrify.ai.backends.base import BackendResponse
        # Actual usage matches the full max_tokens ceiling (worst case).
        return BackendResponse(
            raw_text='{"ok": 1}', input_tokens=10,
            output_tokens=max_tokens, model=model,
        )


def test_concurrent_anthropic_calls_do_not_jointly_exceed_budget(migrated_db, monkeypatch, tmp_path):
    """Two concurrent Anthropic calls, each estimated at $0.60, race against
    a $1.00 daily cap. Jointly they'd spend $1.20 if both were allowed
    through — the fix must reserve the ESTIMATED cost atomically at
    insert-pending time so the second caller's budget check sees the first
    caller's reservation immediately, even though the first caller's
    backend.complete() hasn't returned yet. Uses two independent sqlite
    connections to the SAME file-backed db (like two real worker
    threads/requests each holding their own connection — see db.py) so the
    only thing preventing the race is real cross-connection serialization,
    not test-only in-process sharing."""
    from telemetrify.ai.client import AnthropicClient, BudgetExceeded
    from telemetrify.ai.router import BackendRouter

    monkeypatch.setattr(AnthropicClient, "DEFAULT_DAILY_CAP_USD", 1.0)

    db_path = tmp_path / "data" / "prompts.db"
    conn_b = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn_b.row_factory = sqlite3.Row
    conn_b.execute("PRAGMA journal_mode=WAL")
    conn_b.execute("PRAGMA busy_timeout=30000")

    backend_a = _SlowAnthropicBackend()
    backend_b = _SlowAnthropicBackend()
    router_a = BackendRouter(conn=migrated_db, backends=[backend_a],
                             default_order=["anthropic"])
    router_b = BackendRouter(conn=conn_b, backends=[backend_b],
                             default_order=["anthropic"])

    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def run(name: str, router: "BackendRouter") -> None:
        barrier.wait()
        try:
            router.call(
                feature="qa", template=_make_template(),
                user_kwargs={"who": "x"}, schema=None, max_tokens=40_000,
                model_override="claude-sonnet-4-6",
            )
            results[name] = "accepted"
        except BudgetExceeded:
            results[name] = "rejected"

    t_a = threading.Thread(target=run, args=("A", router_a))
    t_b = threading.Thread(target=run, args=("B", router_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)
    conn_b.close()

    assert sorted(results.values()) == ["accepted", "rejected"], (
        f"exactly one of the two concurrent $0.60 calls must be accepted "
        f"against the $1.00 cap — got {results!r}"
    )

    total_committed = migrated_db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM ai_runs "
        "WHERE backend = 'anthropic' AND status = 'success'"
    ).fetchone()["s"]
    assert total_committed <= 1.0 + 1e-9, (
        f"committed anthropic spend ({total_committed}) must never exceed "
        f"the $1.00 cap — the race let concurrent callers jointly exceed it"
    )


# ---------------------------------------------------------------------------
# BUG 7 — default_router() must reuse backend instances across calls so each
# backend's is_available() 30s TTL cache actually persists.
# ---------------------------------------------------------------------------

def test_default_router_reuses_backend_instances_across_calls():
    """Previously every call to default_router() (which AnthropicClient.call()
    does on EVERY request) constructed brand-new backend objects, so each
    backend's own is_available() cache started empty every time — the
    documented 30s TTL never survived past a single call. Backend instances
    must now be shared process-wide; only the conn-bound router wrapper
    differs per call."""
    import sqlite3 as _sqlite3
    from telemetrify.ai.router import default_router

    conn1 = _sqlite3.connect(":memory:")
    conn2 = _sqlite3.connect(":memory:")
    try:
        r1 = default_router(conn1)
        r2 = default_router(conn2)

        assert r1._by_name["rocco"] is r2._by_name["rocco"], (
            "backend instances must be reused across default_router() calls "
            "so is_available()'s 30s TTL cache actually persists"
        )
        assert r1._by_name["localmac"] is r2._by_name["localmac"]
        assert r1._by_name["ollama"] is r2._by_name["ollama"]
        assert r1._by_name["anthropic"] is r2._by_name["anthropic"]
        assert r1._by_name["claude_cli"] is r2._by_name["claude_cli"]

        # The router wrapper itself is still per-call (conn legitimately
        # differs per request/thread).
        assert r1 is not r2
        assert r1.conn is conn1
        assert r2.conn is conn2
    finally:
        conn1.close()
        conn2.close()
