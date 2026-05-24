"""BackendRouter — composes Rocco vLLM → Ollama Cloud → Anthropic Sonnet.

For each call:
  1. Read the per-feature override (TELEMETRIFY_LLM_ORDER__<feature>) or fall
     back to TELEMETRIFY_LLM_ORDER (default: "rocco,ollama,anthropic").
  2. For each backend in that order:
     a. Skip if `is_available()` is False (no ai_runs row written).
     b. Write a `pending` row to ai_runs with the new `backend` column.
     c. Call `complete()`. On success → parse JSON (if a schema was supplied),
        validate, mark `success`, return.
     d. On transient transport error (ConnectError, TimeoutException, 5xx,
        anthropic.APIConnectionError / APITimeoutError) → mark `failure`,
        fall through to next backend.
     e. On `BudgetExceeded`, schema-parse failure, or schema-validation
        failure → mark `failure`, raise (deterministic — NO fallback).

The router writes ONE row per *attempted* backend so the dashboard renders
the full attempt chain.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import prompts as P
from .backends.base import BackendResponse
from .client import (
    AICallResult,
    BudgetExceeded,
    estimate_cost_usd,
    AnthropicClient,
)


# ── default order ────────────────────────────────────────────────────────
_DEFAULT_ORDER = ("rocco", "ollama", "anthropic")


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
    def _check_budget_for_anthropic(self, estimated_cost_usd: float) -> None:
        client = AnthropicClient(self.conn, override_budget_usd=self.override_budget_usd)
        client._check_budget(estimated_cost_usd)

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
                status: str, error: str | None) -> None:
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        duration_ms = int((time.monotonic() - t0) * 1000)
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
        """Should we fall through to the next backend?"""
        # httpx transport errors
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return True
        # 5xx → transient; 4xx → fatal (do not retry)
        if isinstance(exc, httpx.HTTPStatusError):
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", 0) if resp is not None else 0
            return status >= 500
        # anthropic SDK transport errors (optional import to keep this
        # importable when the SDK isn't installed in some test envs).
        try:
            import anthropic
            if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
                return True
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
    ) -> AICallResult:
        system, user = template.render(**user_kwargs)
        model = model_override or template.model

        order = _order_for(feature, self.default_order)

        last_failure: Exception | None = None

        for backend_name in order:
            backend = self._by_name.get(backend_name)
            if backend is None:
                continue
            try:
                if not backend.is_available():
                    continue
            except Exception:
                continue

            # Budget check only applies to Anthropic (local tiers are ~free).
            if backend_name == "anthropic":
                est_in = (len(system) + len(user)) // 4
                est_out = max_tokens // 2
                est_cost = estimate_cost_usd(model, est_in, est_out)
                self._check_budget_for_anthropic(est_cost)  # raises BudgetExceeded

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
                try:
                    parsed = AnthropicClient._extract_json(resp.raw_text)
                except Exception as e:
                    self._finish(run_id, t0, resp.input_tokens, resp.output_tokens,
                                  cost, "failure", f"bad JSON: {e}")
                    # Deterministic — no fallback on parse failure.
                    raise RuntimeError(
                        f"AI response not JSON: {e}\n---\n{resp.raw_text[:500]}"
                    )

                errs = AnthropicClient.validate_schema(parsed, schema)
                if errs:
                    self._finish(run_id, t0, resp.input_tokens, resp.output_tokens,
                                  cost, "failure",
                                  "schema: " + "; ".join(errs[:3]))
                    raise RuntimeError(
                        f"AI response failed schema validation: {errs[:3]}\n"
                        f"---\n{resp.raw_text[:500]}"
                    )

            self._finish(run_id, t0, resp.input_tokens, resp.output_tokens,
                          cost, "success", None)
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
def default_router(conn: sqlite3.Connection,
                   override_budget_usd: float | None = None) -> BackendRouter:
    """Construct the default 3-tier router from env vars."""
    from .backends.anthropic_backend import AnthropicBackend
    from .backends.openai_compat import OpenAICompatBackend

    rocco_base = os.environ.get("ROCCO_BASE_URL", "http://localhost:18000/v1")
    rocco_model = os.environ.get("ROCCO_MODEL", "moonshotai/Kimi-Dev-72B")
    rocco_key = os.environ.get("ROCCO_API_KEY", "EMPTY")

    ollama_base = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1")
    ollama_key = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    ollama_model = os.environ.get("OLLAMA_CLOUD_MODEL", "gpt-oss:120b")

    rocco = OpenAICompatBackend(
        name="rocco", base_url=rocco_base, api_key=rocco_key,
        default_model=rocco_model,
        input_price_per_m=0.0000002, output_price_per_m=0.0000002,
    )
    ollama = OpenAICompatBackend(
        name="ollama", base_url=ollama_base, api_key=ollama_key,
        default_model=ollama_model,
        input_price_per_m=0.20, output_price_per_m=0.60,
    )
    anthropic = AnthropicBackend()

    return BackendRouter(
        conn=conn,
        backends=[rocco, ollama, anthropic],
        default_order=_DEFAULT_ORDER,
        override_budget_usd=override_budget_usd,
    )
