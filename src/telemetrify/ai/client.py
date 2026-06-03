"""AnthropicClient — the single substrate every AI feature calls through.

Responsibilities:
  - load credentials from env (ANTHROPIC_AUTH_TOKEN || ANTHROPIC_API_KEY)
  - respect ANTHROPIC_BASE_URL (OpenRouter etc.)
  - enforce a daily budget cap via the ai_runs audit table
  - issue messages.create() with retry on transient errors
  - parse + validate the response as JSON against a tiny schema
  - record real cost (input+output tokens × model pricing) on every call
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import prompts as P
from . import schemas as S

# ────────────────────────────────────────────────────────────────────────────
# Pricing table — USD per 1M tokens. Update when models change.
# Conservative defaults (rounded up) for budget accounting.

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model_id_prefix : (input_per_M, output_per_M)
    "claude-haiku-4-5":  (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7":   (15.00, 75.00),
    "claude-opus-4-6":   (15.00, 75.00),
}


def _price_for(model: str) -> tuple[float, float]:
    for prefix, price in MODEL_PRICING.items():
        if model.startswith(prefix):
            return price
    # Unknown model — assume sonnet-tier as a safety default.
    return (3.00, 15.00)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_p, out_p = _price_for(model)
    return (input_tokens * in_p + output_tokens * out_p) / 1_000_000.0


# ────────────────────────────────────────────────────────────────────────────
class BudgetExceeded(RuntimeError):
    """Raised when the daily AI budget cap would be exceeded by the next call."""


@dataclass
class AICallResult:
    parsed: dict
    raw_text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    model: str
    prompt_version: str
    ai_run_id: int


# ────────────────────────────────────────────────────────────────────────────
class AnthropicClient:
    DEFAULT_DAILY_CAP_USD = float(os.environ.get("AI_BUDGET_USD_PER_DAY", "2.00"))

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        override_budget_usd: float | None = None,
    ):
        self.conn = conn
        self.override_budget_usd = override_budget_usd
        self._sdk = None  # lazy

    # ── budget --------------------------------------------------------------
    def _today_spend_usd(self) -> float:
        # Only Anthropic rows count toward the daily $ cap — local tiers
        # (rocco / ollama) ran at ~$0 so charging them against the cap would
        # defeat the whole point of the fall-through chain.
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS s
            FROM ai_runs
            WHERE date(started_at) = date('now')
              AND COALESCE(override_budget, 0) = 0
              AND backend = 'anthropic'
            """
        ).fetchone()
        return float(row["s"] if row else 0.0)

    def _override_spend_usd(self) -> float:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0) AS s
            FROM ai_runs
            WHERE COALESCE(override_budget, 0) = 1
            """
        ).fetchone()
        return float(row["s"] if row else 0.0)

    def _check_budget(self, estimated_cost_usd: float) -> None:
        if self.override_budget_usd is not None:
            already = self._override_spend_usd()
            if already + estimated_cost_usd > self.override_budget_usd:
                raise BudgetExceeded(
                    f"override budget exhausted: {already:.4f}/{self.override_budget_usd:.2f} USD"
                )
            return
        already = self._today_spend_usd()
        cap = self.DEFAULT_DAILY_CAP_USD
        if already + estimated_cost_usd > cap:
            raise BudgetExceeded(
                f"daily budget exhausted: {already:.4f} spent, "
                f"this call estimated {estimated_cost_usd:.4f}, cap {cap:.2f} USD"
            )

    # ── SDK lazy -----------------------------------------------------------
    @staticmethod
    def _load_env_from_claude_settings() -> None:
        """Hydrate ANTHROPIC_* env vars from ~/.claude/settings.json's `env`
        block when the process env is empty / unset for a given key.

        The Claude Code launcher sets these vars in the *child* process but
        sometimes blanks them in sibling shells (empty string is the sandbox
        signal), and the SDK then falls back to api.anthropic.com which is
        wrong for OpenRouter users. settings.json is canonical — prefer it
        over empty/unset env vars, but yield to any non-empty shell value.
        """
        import json as _json
        from pathlib import Path
        path = Path.home() / ".claude" / "settings.json"
        if not path.exists():
            return
        try:
            data = _json.loads(path.read_text())
        except Exception:
            return
        env = (data or {}).get("env") or {}
        for k, v in env.items():
            if not k.startswith("ANTHROPIC_") or not v:
                continue
            # settings.json is canonical for this user's telemetrify
            # integration. Always prefer it over inherited bash env, because
            # the Claude Code launcher commonly injects ANTHROPIC_BASE_URL
            # pointing at the official API for its own use — wrong for us.
            os.environ[k] = v

        # Final fallback — macOS Keychain. Claude Code stores its OAuth
        # access token under the generic-password service "Claude Code
        # -credentials" (a JSON blob: {claudeAiOauth:{accessToken,...}}).
        # Reach for it when both shell env AND settings.json are empty —
        # this is exactly the launchd path that bit us: launchctl
        # inherits a partial env (ANTHROPIC_AUTH_TOKEN="") so the SDK
        # rejected every call with "no API key" and the router declared
        # all backends unavailable.
        AnthropicClient._hydrate_from_macos_keychain()

    @staticmethod
    def _hydrate_from_macos_keychain() -> None:
        """Last-resort credential source for launchd / sandboxed processes
        that don't inherit a shell login env. Reads
            security find-generic-password -s 'Claude Code-credentials' -w
        and surfaces `claudeAiOauth.accessToken` as ANTHROPIC_AUTH_TOKEN.
        No-op when ANTHROPIC_AUTH_TOKEN is already set OR when the
        Keychain query fails (linux/CI/other-OS, item missing, denied).
        """
        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return
        if sys.platform != "darwin":
            return
        try:
            import subprocess as _sp
            import json as _json
            result = _sp.run(
                ["/usr/bin/security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=2.5, check=False,
            )
        except Exception:  # noqa: BLE001 — best-effort, never crash callers
            return
        if result.returncode != 0:
            return
        raw = (result.stdout or "").strip()
        if not raw:
            return
        try:
            blob = _json.loads(raw)
        except _json.JSONDecodeError:
            return
        token = (blob.get("claudeAiOauth") or {}).get("accessToken")
        if token:
            os.environ["ANTHROPIC_AUTH_TOKEN"] = token


    def _client(self):
        if self._sdk is not None:
            return self._sdk
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("anthropic SDK not installed; add it to pyproject.toml") from e
        self._load_env_from_claude_settings()
        # launchd-spawned processes inherit a partial environment from
        # launchctl that includes ANTHROPIC_API_KEY="" (the Claude Code
        # sandbox signal — empty string, not unset). The Anthropic SDK's
        # constructor treats a present env var as authoritative even when
        # it's empty, so it would happily send `x-api-key: ""` to
        # api.anthropic.com — yielding 401 "invalid x-api-key" even
        # though we've explicitly passed `auth_token=` for the OAuth
        # bearer path. Strip empties here so the SDK falls back cleanly
        # to whichever kwarg we actually pass.
        for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if os.environ.get(_k, None) == "":
                os.environ.pop(_k, None)
        # Auth precedence:
        #   ANTHROPIC_AUTH_TOKEN → SDK `auth_token=` → `Authorization: Bearer …`
        #       (OAuth access tokens from Claude Code's Keychain, `sk-ant-oat-…`,
        #       OpenRouter / vLLM proxies that expect a bearer)
        #   ANTHROPIC_API_KEY    → SDK `api_key=`     → `x-api-key: …`
        #       (classic Anthropic console keys, `sk-ant-api-…`)
        # Mixing these up is exactly what made the keychain-hydrated OAuth
        # token come back with 401 "invalid x-api-key" — the SDK was happily
        # putting an OAuth bearer into the wrong header.
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
        api_key    = os.environ.get("ANTHROPIC_API_KEY") or ""
        if not auth_token and not api_key:
            raise RuntimeError(
                "no API key — set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY "
                "(or populate the `env` block in ~/.claude/settings.json, "
                "or log into Claude Code so its Keychain entry is readable)"
            )
        kwargs: dict = {}
        if auth_token:
            kwargs["auth_token"] = auth_token
            # Required to unlock OAuth-bearer auth on /v1/messages.
            # Without it, api.anthropic.com falls back to expecting
            # x-api-key and returns 401 "invalid x-api-key" — exactly
            # the symptom that exposed this whole bug. Empirically the
            # only beta gate we need; safe to send unconditionally.
            kwargs["default_headers"] = {
                "anthropic-beta": "oauth-2025-04-20",
            }
        else:
            kwargs["api_key"] = api_key
        if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
            kwargs["base_url"] = base_url
        self._sdk = anthropic.Anthropic(**kwargs)
        return self._sdk

    # ── schema validation --------------------------------------------------
    @staticmethod
    def validate_schema(parsed: Any, schema: dict, path: str = "$") -> list[str]:
        errors: list[str] = []
        if not isinstance(parsed, dict):
            return [f"{path}: expected object, got {type(parsed).__name__}"]
        for key, spec in schema.items():
            if key not in parsed:
                errors.append(f"{path}.{key}: missing")
                continue
            v = parsed[key]
            t = spec.get("type")
            p = f"{path}.{key}"
            if t == "int":
                if not isinstance(v, int) or isinstance(v, bool):
                    errors.append(f"{p}: expected int, got {type(v).__name__}")
                elif "min" in spec and v < spec["min"]:
                    errors.append(f"{p}: < min {spec['min']}")
                elif "max" in spec and v > spec["max"]:
                    errors.append(f"{p}: > max {spec['max']}")
            elif t == "float":
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    errors.append(f"{p}: expected number, got {type(v).__name__}")
                elif "min" in spec and v < spec["min"]:
                    errors.append(f"{p}: < min {spec['min']}")
                elif "max" in spec and v > spec["max"]:
                    errors.append(f"{p}: > max {spec['max']}")
            elif t == "str":
                if not isinstance(v, str):
                    errors.append(f"{p}: expected str, got {type(v).__name__}")
                elif "max_len" in spec and len(v) > spec["max_len"]:
                    errors.append(f"{p}: len {len(v)} > {spec['max_len']}")
            elif t == "bool":
                if not isinstance(v, bool):
                    errors.append(f"{p}: expected bool, got {type(v).__name__}")
            elif t == "enum":
                if v not in spec["values"]:
                    errors.append(f"{p}: '{v}' not in {spec['values']}")
            elif t == "obj":
                if not isinstance(v, dict):
                    errors.append(f"{p}: expected object")
                elif spec.get("fields"):
                    errors.extend(AnthropicClient.validate_schema(v, spec["fields"], p))
        return errors

    @staticmethod
    def _extract_json(text: str) -> Any:
        """Strip code fences and surrounding prose; return parsed JSON."""
        t = text.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else t
            if t.endswith("```"):
                t = t.rsplit("```", 1)[0]
            elif "```" in t:
                t = t.rsplit("```", 1)[0]
        # Find first { and last }
        start = t.find("{")
        end = t.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object in response")
        return json.loads(t[start : end + 1])

    # ── core call ----------------------------------------------------------
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
    ) -> "AICallResult":
        """Thin compatibility shim — delegates to BackendRouter so all nine
        feature modules keep working unchanged while gaining the 3-tier
        fall-through (Rocco vLLM → Ollama Cloud → Anthropic Sonnet).

        `order`, when given, pins the tier sequence for this one call
        (overriding the feature's env default) — see qa.plan()."""
        from .router import default_router
        router = default_router(self.conn, override_budget_usd=self.override_budget_usd)
        return router.call(
            feature=feature,
            template=template,
            user_kwargs=user_kwargs,
            schema=schema,
            target_id=target_id,
            max_tokens=max_tokens,
            timeout=timeout,
            model_override=model_override,
            order=order,
        )

    def _finish(self, ai_run_id: int, started: str, t0: float,
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
