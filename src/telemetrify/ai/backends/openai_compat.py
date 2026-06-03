"""OpenAICompatBackend — one class serves both vLLM (Rocco) and Ollama Cloud.

Uses `httpx` (already a dep) to avoid pulling the official `openai` SDK.
Sends BOTH `extra_body={"guided_json": schema}` (vLLM honors this) AND
`response_format={"type": "json_object"}` (Ollama Cloud honors this) when a
schema is passed — each endpoint ignores the hint it doesn't understand.

`is_available()` pings `GET {base_url}/models` with a 1s timeout and caches
the result for ~30s so the router doesn't burn time on every call.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .base import BackendResponse


# How long to trust a successful is_available() probe. Seconds.
_AVAIL_CACHE_TTL_S = 30.0
_AVAIL_PROBE_TIMEOUT_S = 1.0


class OpenAICompatBackend:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        default_model: str,
        input_price_per_m: float,
        output_price_per_m: float,
        request_timeout_s: float = 60.0,
    ) -> None:
        self.name = name
        # Trim trailing slash so we always join cleanly.
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.input_price_per_m = input_price_per_m
        self.output_price_per_m = output_price_per_m
        self.request_timeout_s = request_timeout_s
        self._avail_cache: tuple[float, bool] | None = None  # (expires_at, value)

    # ── availability probe (cached) ─────────────────────────────────────
    def is_available(self) -> bool:
        now = time.monotonic()
        if self._avail_cache is not None and self._avail_cache[0] > now:
            return self._avail_cache[1]
        if not self.api_key:
            self._avail_cache = (now + _AVAIL_CACHE_TTL_S, False)
            return False
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/models"
        ok = False
        try:
            with httpx.Client(timeout=_AVAIL_PROBE_TIMEOUT_S) as c:
                resp = c.get(url, headers=headers)
                ok = 200 <= getattr(resp, "status_code", 0) < 300
        except (httpx.ConnectError, httpx.TimeoutException,
                 httpx.HTTPError, Exception):
            ok = False
        self._avail_cache = (now + _AVAIL_CACHE_TTL_S, ok)
        return ok

    # ── core call ───────────────────────────────────────────────────────
    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        json_schema: Any | None,
        timeout: float | None = None,
    ) -> BackendResponse:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Cross-vendor model substitution: callers pass the PROMPT
        # TEMPLATE's model id (e.g. "claude-haiku-4-5") which is
        # Anthropic-vocabulary. An OpenAI-compat endpoint (Ollama,
        # vLLM, etc.) doesn't know those names and returns 404.
        # Always prefer this backend's own configured default_model
        # when set — the template's model is a hint, not a directive.
        effective_model = self.default_model or model
        body: dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            # Dual hint: Ollama Cloud honors response_format; vLLM honors
            # extra_body.guided_json. Each ignores the other.
            body["response_format"] = {"type": "json_object"}
            body["extra_body"] = {"guided_json": json_schema}

        with httpx.Client(timeout=timeout if timeout else self.request_timeout_s) as c:
            resp = c.post(url, headers=headers, json=body)
            status = getattr(resp, "status_code", 0)
            if status >= 400:
                # Build an HTTPStatusError so the router can distinguish
                # 4xx (no fallback) from 5xx (fall through).
                raise httpx.HTTPStatusError(
                    f"{status} from {self.name}",
                    request=getattr(resp, "request", None),
                    response=resp,
                )
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"{self.name}: empty choices in response")
        msg = choices[0].get("message") or {}
        raw_text = msg.get("content") or ""

        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)

        return BackendResponse(
            raw_text=raw_text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=body["model"],
        )
