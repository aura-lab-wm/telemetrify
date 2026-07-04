"""OpenAICompatBackend — one class serves both vLLM (Rocco) and Ollama Cloud.

Uses `httpx` (already a dep) to avoid pulling the official `openai` SDK.

Guided/structured JSON hint (BUG 2, 2026-07 audit — rewritten):
The previous code sent `extra_body={"guided_json": schema}`. That shape only
means something to the *official* `openai` Python SDK, which unpacks
`extra_body`'s keys into the top level of the outgoing JSON before sending
it — httpx has no such magic, so the wire body actually contained a literal
nested `"extra_body"` key that vLLM's OpenAI-compat server doesn't recognize
and silently ignores. It also passed telemetrify's own internal mini-schema
DSL (see ai/schemas.py — `{"type": "int", "min":.., ...}`) straight through,
which isn't valid JSON Schema either.

Verified LIVE against the actual rocco deployment (2026-07-04, vLLM 0.20.2,
model moonshotai/Kimi-Dev-72B via SSH to the rocco host) with three shapes:
  - top-level `guided_json: <schema>`               → NOT enforced (no-op)
  - `extra_body: {"guided_json": <schema>}` (old)    → NOT enforced (no-op)
  - `response_format: {"type": "json_schema",
                        "json_schema": {"name": ..., "schema": <schema>}}`
                                                      → ACTUALLY enforced
    (confirmed by forcing an enum to a single unnatural value the model
    would never pick unprompted, e.g. `{"enum": ["chartreuse"]}`, and
    getting back exactly `{"color": "chartreuse"}`, finish_reason "stop").
So for the rocco tier we now send that verified shape, translating the
internal DSL into real JSON Schema first (`_dsl_to_json_schema`). This is
specific to rocco's *current* vLLM build — if that build is upgraded, this
wire contract should be re-verified the same way (see module docstring / PR
notes; do not assume it holds forever).

Ollama (local + Cloud) is untouched: it still gets
`response_format={"type": "json_object"}`, which was already verified to
work for it and is unrelated to this bug (the audit's BUG 2 was scoped to
the rocco/vLLM tier specifically).

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


def _dsl_field_to_json_schema(spec: dict) -> dict:
    """Convert one field spec of telemetrify's internal mini-schema DSL (see
    ai/schemas.py: `{"type": "int"|"float"|"str"|"bool"|"enum"|"obj", ...}`)
    into a real JSON Schema fragment. Deliberately permissive — no
    `additionalProperties: false` anywhere — so this never forbids extra
    keys a caller's schema didn't explicitly account for (e.g. DIGEST's
    advisory top_clusters/regressions/suggestions fields, which schemas.py
    notes are intentionally left unconstrained).
    """
    t = spec.get("type")
    if t == "int":
        out: dict = {"type": "integer"}
    elif t == "float":
        out = {"type": "number"}
    elif t == "bool":
        return {"type": "boolean"}
    elif t == "str":
        out = {"type": "string"}
        if "max_len" in spec:
            out["maxLength"] = spec["max_len"]
        return out
    elif t == "enum":
        return {"enum": list(spec.get("values", []))}
    elif t == "obj":
        fields = spec.get("fields") or {}
        if not fields:
            # Opaque nested object (e.g. QA_PLANNER's "filters") — allow any
            # shape rather than guess wrong.
            return {"type": "object"}
        return {
            "type": "object",
            "properties": {k: _dsl_field_to_json_schema(v) for k, v in fields.items()},
            "required": list(fields.keys()),
        }
    else:
        # Unknown spec type — don't constrain this field at all rather than
        # risk guiding it wrong.
        return {}
    if "min" in spec:
        out["minimum"] = spec["min"]
    if "max" in spec:
        out["maximum"] = spec["max"]
    return out


def _dsl_to_json_schema(dsl_schema: dict) -> dict:
    """Convert a top-level telemetrify mini-schema DSL dict (the shape every
    caller in ai/schemas.py uses) into a real JSON Schema object suitable
    for vLLM's `response_format: {"type": "json_schema", ...}` guided
    decoding. Every top-level key is required (validate_schema() in
    ai/client.py treats every declared key as mandatory)."""
    return {
        "type": "object",
        "properties": {k: _dsl_field_to_json_schema(v) for k, v in dsl_schema.items()},
        "required": list(dsl_schema.keys()),
    }


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
            if self.name == "rocco":
                # The rocco tier speaks vLLM's OpenAI-compat server — see the
                # module docstring for the live verification behind this
                # exact shape (2026-07-04, vLLM 0.20.2). Best-effort: the DSL
                # converter is small and self-contained, but if it ever
                # trips on a schema shape it doesn't recognize, fail safe by
                # NOT claiming to guide the output rather than send
                # something malformed (BUG 2's explicit fallback).
                try:
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "response",
                            "schema": _dsl_to_json_schema(json_schema),
                        },
                    }
                except Exception:
                    pass
            else:
                # localmac / ollama (genuine Ollama endpoints) — unchanged,
                # already-verified hint; out of scope for BUG 2, which was
                # specifically about the rocco/vLLM wire shape.
                body["response_format"] = {"type": "json_object"}

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
        # "stop" | "length" | "tool_calls" | "content_filter" — see BUG 4:
        # surfaced so a max_tokens truncation isn't silently lost.
        stop_reason = choices[0].get("finish_reason")

        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)

        return BackendResponse(
            raw_text=raw_text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=body["model"],
            stop_reason=stop_reason,
        )
