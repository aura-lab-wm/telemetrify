"""BackendRouter — composes Rocco vLLM → Ollama Cloud → Anthropic Sonnet.

For each call:
  1. Read the per-feature override (TELEMETRIFY_LLM_ORDER__<feature>) or fall
     back to TELEMETRIFY_LLM_ORDER (default: "rocco,localmac,ollama,anthropic").
  2. For each backend in that order:
     a. Skip if `is_available()` is False (no ai_runs row written).
     b. For the `anthropic` tier only: atomically check the daily/override
        budget AND reserve the ESTIMATED cost by inserting the `pending` row
        with cost_usd=estimate (see `_reserve_anthropic_budget_and_insert_pending`)
        — this closes the check-then-act race where two concurrent Anthropic
        calls could both pass the budget check before either's real cost was
        recorded. If the estimate would exceed budget, a row IS still written
        (status='over_budget') before raising — every attempted backend gets
        a row, even a rejected one. Other tiers just get a plain `pending`
        row (local tiers are ~free and don't count toward the cap).
     c. Call `complete()`. On success → parse JSON (if a schema was supplied),
        validate, mark `success` (persisting the model that ACTUALLY served
        the call — `resp.model` — not just the one originally requested,
        since a backend may substitute its own configured model), return.
     d. On transient transport error (ConnectError, TimeoutException, 5xx,
        429, 401/403 auth failure scoped to this tier,
        anthropic.APIConnectionError / APITimeoutError) → mark `failure`,
        fall through to next backend.
     e. On `BudgetExceeded` (pre-flight, row already recorded as
        'over_budget'), schema-parse failure, or schema-validation failure
        → mark `failure` (or leave as 'over_budget'), raise (deterministic —
        NO fallback).

The router writes ONE row per *attempted* backend so the dashboard renders
the full attempt chain.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import prompts as P
from .backends.base import BackendResponse, BackendTransient
from .client import (
    AICallResult,
    BudgetExceeded,
    estimate_cost_usd,
    AnthropicClient,
)


# ── default order ────────────────────────────────────────────────────────
_DEFAULT_ORDER = ("rocco", "localmac", "ollama", "anthropic")


def _log(msg: str) -> None:
    """Cheap diagnostic line — goes to stderr (visible in launchd's
    ui-stderr.log and in bin/insights' nohup log) so we can debug
    "why did the router pick X" without strace. Tagged so it's easy
    to grep for: `grep '[router]' data/ui-stderr.log`."""
    import sys
    print(f"[router] {msg}", file=sys.stderr, flush=True)


def _order_for(feature: str, default: list[str] | tuple[str, ...]) -> list[str]:
    """Resolve `TELEMETRIFY_LLM_ORDER__<feature>` → falls back to
    `TELEMETRIFY_LLM_ORDER` → falls back to the supplied default."""
    per_feature = os.environ.get(f"TELEMETRIFY_LLM_ORDER__{feature}")
    if per_feature:
        return [s.strip() for s in per_feature.split(",") if s.strip()]
    global_env = os.environ.get("TELEMETRIFY_LLM_ORDER")
    if global_env:
        return [s.strip() for s in global_env.split(",") if s.strip()]
    return list(default)


# ── router ───────────────────────────────────────────────────────────────
class BackendRouter:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        backends: list,
        default_order: list[str] | tuple[str, ...] = _DEFAULT_ORDER,
        override_budget_usd: float | None = None,
    ) -> None:
        self.conn = conn
        self._by_name = {b.name: b for b in backends}
        self.default_order = tuple(default_order)
        self.override_budget_usd = override_budget_usd

    # ── budget (only Anthropic counts toward cap) ───────────────────────
    def _reserve_anthropic_budget_and_insert_pending(
        self,
        *,
        feature: str,
        model: str,
        prompt_version: str,
        target_id: str | int | None,
        estimated_cost_usd: float,
    ) -> tuple[int, str]:
        """Atomically (a) check whether `estimated_cost_usd` would blow the
        daily/override budget and (b) reserve it by inserting the `pending`
        ai_runs row with cost_usd = estimated_cost_usd — all inside ONE
        BEGIN IMMEDIATE transaction.

        BUG 2 fix (TOCTOU race): the old flow called `_check_budget` (a bare
        SELECT), and only AFTERWARDS inserted a pending row with cost_usd=0.
        Two concurrent Anthropic calls could both run that SELECT and both
        see the same "already spent" total before either's real cost was
        ever recorded, so both would pass the check and jointly exceed
        AI_BUDGET_USD_PER_DAY. BEGIN IMMEDIATE grabs sqlite's write lock
        *before* we read the current spend, so a second connection's own
        BEGIN IMMEDIATE (a concurrent request on another thread/connection)
        blocks — up to PRAGMA busy_timeout — until we COMMIT our
        reservation. That turns "read spend → decide → write" into one
        atomic unit even across separate sqlite3.Connection objects, and the
        next caller's read sees OUR reservation immediately, before our real
        backend call has even started. `_finish()` later corrects cost_usd
        down (or up) to the real spend once the call completes.

        BUG 4 fix (silent rejection): if the estimate would exceed budget, we
        still INSERT a row (status='over_budget') instead of raising with
        nothing persisted — this module's own contract is that every
        *attempted* backend gets a row, including a rejected one.
        """
        client = AnthropicClient(self.conn, override_budget_usd=self.override_budget_usd)
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        override_flag = 1 if self.override_budget_usd is not None else 0
        target = str(target_id) if target_id is not None else None

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            client._check_budget(estimated_cost_usd)  # raises BudgetExceeded
        except BudgetExceeded as e:
            self.conn.execute(
                """
                INSERT INTO ai_runs(feature, model, prompt_version, target_id,
                                    input_tokens, output_tokens, cost_usd,
                                    status, error, started_at, finished_at,
                                    override_budget, backend)
                VALUES (?, ?, ?, ?, 0, 0, 0, 'over_budget', ?, ?, ?, ?, 'anthropic')
                """,
                (feature, model, prompt_version, target,
                 str(e), started, started, override_flag),
            )
            self.conn.commit()
            raise
        except Exception:
            # Unexpected (non-budget) error while we hold the write lock —
            # release it so we don't wedge every other connection's own
            # BEGIN IMMEDIATE behind a lock nobody will ever release.
            self.conn.execute("ROLLBACK")
            raise

        try:
            cur = self.conn.execute(
                """
                INSERT INTO ai_runs(feature, model, prompt_version, target_id,
                                    input_tokens, output_tokens, cost_usd, status,
                                    started_at, override_budget, backend)
                VALUES (?, ?, ?, ?, 0, 0, ?, 'pending', ?, ?, 'anthropic')
                """,
                (feature, model, prompt_version, target,
                 estimated_cost_usd, started, override_flag),
            )
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.commit()
        return cur.lastrowid, started

    # ── ai_runs row management ──────────────────────────────────────────
    def _insert_pending(
        self,
        *,
        feature: str,
        model: str,
        prompt_version: str,
        target_id: str | int | None,
        backend_name: str,
    ) -> tuple[int, str]:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self.conn.execute(
            """
            INSERT INTO ai_runs(feature, model, prompt_version, target_id,
                                input_tokens, output_tokens, cost_usd, status,
                                started_at, override_budget, backend)
            VALUES (?, ?, ?, ?, 0, 0, 0, 'pending', ?, ?, ?)
            """,
            (feature, model, prompt_version,
             str(target_id) if target_id is not None else None,
             started,
             1 if self.override_budget_usd is not None else 0,
             backend_name),
        )
        self.conn.commit()
        return cur.lastrowid, started

    def _finish(self, ai_run_id: int, t0: float,
                in_tok: int, out_tok: int, cost: float,
                status: str, error: str | None,
                model: str | None = None) -> None:
        """Close out an ai_runs row. `model`, when given, is the model that
        ACTUALLY served the request (`resp.model` — e.g. a local backend may
        substitute its own configured model regardless of what the template
        requested) and overwrites the `model` column, which was written at
        insert-pending time with only the *requested* model. BUG 1 fix: every
        fallback-tier row used to stay mislabeled with the originally
        requested model forever, since nothing ever corrected it here."""
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        duration_ms = int((time.monotonic() - t0) * 1000)
        if model is not None:
            self.conn.execute(
                """
                UPDATE ai_runs
                SET input_tokens = ?, output_tokens = ?, cost_usd = ?,
                    status = ?, error = ?, finished_at = ?, duration_ms = ?,
                    model = ?
                WHERE id = ?
                """,
                (in_tok, out_tok, cost, status, error, finished, duration_ms,
                 model, ai_run_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE ai_runs
                SET input_tokens = ?, output_tokens = ?, cost_usd = ?,
                    status = ?, error = ?, finished_at = ?, duration_ms = ?
                WHERE id = ?
                """,
                (in_tok, out_tok, cost, status, error, finished, duration_ms, ai_run_id),
            )
        self.conn.commit()

    # ── transient-error classification ──────────────────────────────────
    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        """Should we fall through to the next backend?

        Transient = "this backend can't serve us RIGHT NOW but the
        request itself is fine" — so retrying on a different backend
        is meaningful. Fatal = "the request is broken in a way no
        backend will accept" — falling through wastes everyone's
        quota on the same bad payload.

        Categories:
          - Transport errors (ConnectError, TimeoutException)         → transient
          - 5xx server errors                                         → transient
          - 429 rate-limit                                            → transient
            (specifically the OAuth-tier bucket exhaustion we hit when
            the user's Claude subscription quota is shared between
            Claude Code and telemetrify; the NEXT backend may still
            have budget)
          - 503 service unavailable                                   → transient
          - 401 / 403 (auth failure ON THIS TIER)                     → transient
            A 401/403 means THIS backend's credentials are broken (a
            rotated/expired key, a misconfigured token for just this one
            tier) — it says nothing about whether the REQUEST is valid.
            The next tier authenticates independently (its own API key /
            OAuth token / local no-auth), so it deserves its own shot
            rather than being killed by a problem scoped to one backend.
            This is distinct from a 400 malformed-request (below), which
            is a property of the PAYLOAD and would fail identically on
            every tier — falling through there just burns everyone's
            quota on the same broken request.
          - 4xx other than 429 / 401 / 403                            → fatal
          - everything else                                           → fatal
        """
        # Backend self-classified the failure as recoverable (e.g. the
        # claude-cli tier: binary missing, non-zero exit, subprocess timeout,
        # error envelope) — fall through to the next tier.
        if isinstance(exc, BackendTransient):
            return True
        # httpx transport errors
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", 0) if resp is not None else 0
            return status >= 500 or status in (429, 401, 403)
        # anthropic SDK errors — optional import so the router stays
        # importable in test envs without the SDK installed.
        try:
            import anthropic
            if isinstance(exc, (anthropic.APIConnectionError,
                                anthropic.APITimeoutError,
                                anthropic.RateLimitError)):
                return True
            # Anthropic raises APIStatusError for non-2xx; inspect the code.
            api_status_err = getattr(anthropic, "APIStatusError", None)
            if api_status_err and isinstance(exc, api_status_err):
                code = getattr(exc, "status_code", 0)
                return code >= 500 or code in (429, 401, 403)
        except Exception:
            pass
        return False

    # ── core call ───────────────────────────────────────────────────────
    def call(
        self,
        *,
        feature: str,
        template: P.PromptTemplate,
        user_kwargs: dict,
        schema: dict | None,
        target_id: str | int | None = None,
        max_tokens: int = 1024,
        timeout: float = 30.0,
        model_override: str | None = None,
        order: list[str] | None = None,
    ) -> AICallResult:
        system, user = template.render(**user_kwargs)
        model = model_override or template.model

        # An explicit `order` pins the tier sequence for THIS call regardless of
        # the feature's env default — used by the /ask planner, which needs the
        # reliable-JSON claude_cli tier even though the synthesizer (same
        # feature="qa") is happy on the faster local tier.
        if order:
            order = list(order)
        else:
            order = _order_for(feature, self.default_order)

        last_failure: Exception | None = None

        for backend_name in order:
            backend = self._by_name.get(backend_name)
            if backend is None:
                _log(f"skip {backend_name}: not registered")
                continue
            try:
                if not backend.is_available():
                    # User-visible diagnostic: lets us reason about
                    # WHY a request landed on a particular tier. The
                    # "fans spinning because localmac got picked"
                    # incident would have been resolved instantly
                    # with this line.
                    _log(f"skip {backend_name}: is_available()=False")
                    continue
            except Exception as _e:
                _log(f"skip {backend_name}: is_available() raised {_e}")
                continue
            _log(f"try {backend_name} ({model})")

            # Budget check only applies to Anthropic (local tiers are ~free).
            if backend_name == "anthropic":
                est_in = (len(system) + len(user)) // 4
                # BUG 6: estimate output at the full max_tokens ceiling, not
                # max_tokens // 2. A completion can legitimately run close to
                # max_tokens; gating on half of it let real (non-racing)
                # usage alone blow past the cap by ~2x. max_tokens IS the
                # hard API ceiling, so using it directly is the tightest
                # bound that can never be exceeded by the output side.
                est_out = max_tokens
                est_cost = estimate_cost_usd(model, est_in, est_out)
                # BUG 2 + BUG 4: check-and-reserve atomically, and persist a
                # row even on rejection — see the method's docstring.
                run_id, _started = self._reserve_anthropic_budget_and_insert_pending(
                    feature=feature, model=model,
                    prompt_version=template.version, target_id=target_id,
                    estimated_cost_usd=est_cost,
                )  # raises BudgetExceeded
            else:
                run_id, _started = self._insert_pending(
                    feature=feature, model=model,
                    prompt_version=template.version, target_id=target_id,
                    backend_name=backend_name,
                )

            t0 = time.monotonic()
            try:
                resp: BackendResponse = backend.complete(
                    system=system, user=user, model=model,
                    max_tokens=max_tokens, json_schema=schema,
                    timeout=timeout,
                )
            except BudgetExceeded as e:
                # Should not happen here (we pre-checked) but treat as fatal.
                self._finish(run_id, t0, 0, 0, 0.0, "failure", f"budget: {e}")
                raise
            except Exception as e:
                self._finish(run_id, t0, 0, 0, 0.0, "failure", str(e))
                if self._is_transient(e):
                    last_failure = e
                    continue
                # 4xx / unexpected → deterministic failure, do NOT fall through.
                raise

            # Cost calc — local backends use micro-pricing, Anthropic uses
            # the shared pricing table.
            if backend_name == "anthropic":
                cost = estimate_cost_usd(model, resp.input_tokens, resp.output_tokens)
            else:
                # Best-effort: ask the backend if it exposes pricing.
                in_p = float(getattr(backend, "input_price_per_m", 0.0))
                out_p = float(getattr(backend, "output_price_per_m", 0.0))
                cost = (resp.input_tokens * in_p + resp.output_tokens * out_p) / 1_000_000.0

            # Parse JSON if a schema was supplied (synthesizer passes None).
            parsed: dict | Any = {}
            if schema is not None:
                schema_error: str | None = None
                try:
                    parsed = AnthropicClient._extract_json(resp.raw_text)
                except Exception as e:
                    schema_error = f"bad JSON: {e}"
                else:
                    errs = AnthropicClient.validate_schema(parsed, schema)
                    if errs:
                        schema_error = "schema: " + "; ".join(errs[:3])

                if schema_error is not None:
                    self._finish(run_id, t0, resp.input_tokens, resp.output_tokens,
                                  cost, "failure", schema_error, model=resp.model)
                    # A weak tier (e.g. local Ollama) returning prose / invalid
                    # JSON is NOT a broken request — the payload is fine, the
                    # backend just couldn't follow the schema. Fall through so a
                    # stronger tier can satisfy it, instead of hard-failing the
                    # whole call (the bug that broke /ask + the bulk graders the
                    # moment a local model came online). If EVERY tier fails to
                    # produce valid JSON, last_failure is raised after the loop.
                    last_failure = RuntimeError(
                        f"{backend_name} {schema_error}\n---\n{resp.raw_text[:500]}"
                    )
                    continue

            self._finish(run_id, t0, resp.input_tokens, resp.output_tokens,
                          cost, "success", None, model=resp.model)
            duration_ms = int((time.monotonic() - t0) * 1000)

            return AICallResult(
                parsed=parsed if isinstance(parsed, dict) else {},
                raw_text=resp.raw_text,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cost_usd=cost,
                duration_ms=duration_ms,
                model=resp.model,
                prompt_version=template.version,
                ai_run_id=run_id,
            )

        # No backend in the order succeeded.
        if last_failure is not None:
            raise RuntimeError(
                f"all backends in order {order!r} failed; last error: {last_failure}"
            ) from last_failure
        raise RuntimeError(
            f"no available backend in order {order!r} for feature={feature!r}"
        )


# ── factory ──────────────────────────────────────────────────────────────
# BUG 7 fix: `default_router()` used to build brand-new backend objects on
# EVERY call (AnthropicClient.call() constructs a fresh BackendRouter per
# request). Each backend's is_available() has its own ~30s TTL cache, but
# that cache lives on the instance — a fresh instance means a fresh, empty
# cache, so the documented 30s TTL was dead in production: every single AI
# request re-probed rocco/localmac/ollama from scratch. Backends are pure
# config objects keyed off env vars (they don't hold `conn` or any per-call
# state), so it's safe to build them ONCE per process and hand the SAME
# instances to every BackendRouter — only the (cheap, conn-bound) router
# wrapper is rebuilt per call, which is correct since `conn` legitimately
# differs per call/thread (see db.py's per-thread connection cache).
_backend_cache: dict[str, Any] | None = None
_backend_cache_lock = threading.Lock()


def _build_backends() -> dict[str, Any]:
    """Construct the 5 backend instances fresh from env vars. Called at most
    once per process — see `_cached_backends()`."""
    from .backends.anthropic_backend import AnthropicBackend
    from .backends.claude_cli import ClaudeCLIBackend
    from .backends.openai_compat import OpenAICompatBackend

    rocco_base = os.environ.get("ROCCO_BASE_URL", "http://localhost:18000/v1")
    rocco_model = os.environ.get("ROCCO_MODEL", "moonshotai/Kimi-Dev-72B")
    rocco_key = os.environ.get("ROCCO_API_KEY", "EMPTY")

    localmac_base = os.environ.get(
        "OLLAMA_LOCAL_BASE_URL", "http://localhost:11434/v1")
    # Default to gpt-oss:20b: it's the same model-family the Rocco hook
    # already recommends, the user's likely-installed via the prompt
    # workflow, and 20B parameters synthesizes decent /ask answers at
    # ~25 tok/s on M-series Apple Silicon. Override via env when a
    # smaller (qwen3:1.7b for speed) or larger (gemma3:12b for quality)
    # model is preferred.
    localmac_model = os.environ.get("OLLAMA_LOCAL_MODEL", "gpt-oss:20b")

    ollama_base = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1")
    ollama_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    ollama_model = os.environ.get("OLLAMA_CLOUD_MODEL", "gpt-oss:120b")

    rocco = OpenAICompatBackend(
        name="rocco", base_url=rocco_base, api_key=rocco_key,
        default_model=rocco_model,
        input_price_per_m=0.0000002, output_price_per_m=0.0000002,
    )
    # Local Ollama uses the same OpenAI-compat protocol — no new backend
    # class needed. `EMPTY` API key is fine; Ollama ignores it. Synthetic
    # micro-cost so the ai_runs dashboard distinguishes localmac from
    # truly-no-op rows.
    localmac = OpenAICompatBackend(
        name="localmac", base_url=localmac_base, api_key="EMPTY",
        default_model=localmac_model,
        input_price_per_m=0.0000001, output_price_per_m=0.0000001,
    )
    ollama = OpenAICompatBackend(
        name="ollama", base_url=ollama_base, api_key=ollama_key,
        default_model=ollama_model,
        input_price_per_m=0.20, output_price_per_m=0.60,
    )
    anthropic = AnthropicBackend()
    # Headless `claude -p` tier. NOT in _DEFAULT_ORDER (it's slow + loads the
    # full Claude Code system prompt), so bulk features never reach for it.
    # It's selected explicitly per-feature via TELEMETRIFY_LLM_ORDER__<feature>
    # — notably qa (/ask), whose PLANNER needs strict JSON that the local
    # Ollama tier can't be trusted to produce.
    claude_cli = ClaudeCLIBackend()

    return {
        "rocco": rocco, "localmac": localmac, "ollama": ollama,
        "anthropic": anthropic, "claude_cli": claude_cli,
    }


def _cached_backends() -> dict[str, Any]:
    """Return the process-wide backend instances, building them at most
    once (double-checked locking — cheap since this is only contended for
    the first call of the process)."""
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache
    with _backend_cache_lock:
        if _backend_cache is None:
            _backend_cache = _build_backends()
        return _backend_cache


def default_router(conn: sqlite3.Connection,
                   override_budget_usd: float | None = None) -> BackendRouter:
    """Construct the default 4-tier router from env vars.

    Tier order (highest priority → lowest):
      1. rocco     — vLLM (Kimi-72B) on the GPU box via SSH tunnel; free
      2. localmac  — Ollama running on THIS Mac at localhost:11434
                     Brand new tier — makes /ask survive both "Rocco GPUs
                     all busy" AND "Anthropic OAuth bucket exhausted".
                     Auto-skipped (is_available=False) when ollama isn't
                     installed/running locally. Recommended local model:
                       ollama pull qwen2.5:3b-instruct
                       ollama pull llama3.2:3b
                     ~2 GB on disk; runs at 50+ tok/s on M-series Apple Silicon.
      3. ollama    — Ollama Cloud (https://ollama.com/v1); paid, optional.
      4. anthropic — final fallback; uses your Claude Code OAuth bucket OR
                     ANTHROPIC_API_KEY if you set one in settings.json.

    The backend instances themselves are cached process-wide (see
    `_cached_backends()`) so each backend's is_available() 30s TTL cache
    actually persists across calls; only this thin, conn-bound
    `BackendRouter` wrapper is built fresh per call.
    """
    backends_by_name = _cached_backends()
    return BackendRouter(
        conn=conn,
        backends=list(backends_by_name.values()),
        default_order=_DEFAULT_ORDER,
        override_budget_usd=override_budget_usd,
    )
