"""AnthropicBackend — wraps the official anthropic SDK.

Lazy SDK construction so import-time has no side effects. `is_available()`
returns True iff an API key is present in env (or ~/.claude/settings.json).
"""
from __future__ import annotations

import os
from typing import Any

from .base import BackendResponse


class AnthropicBackend:
    name = "anthropic"

    def __init__(self) -> None:
        self._sdk = None

    # ── availability ─────────────────────────────────────────────────────
    def is_available(self) -> bool:
        # Hydrate from settings.json + macOS Keychain if the shell env is
        # empty (launchd-spawned processes inherit a partial env).
        from ..client import AnthropicClient
        try:
            AnthropicClient._load_env_from_claude_settings()
        except Exception:
            pass
        return bool(
            os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
        )

    # ── SDK lazy ─────────────────────────────────────────────────────────
    def _client(self):
        if self._sdk is not None:
            return self._sdk
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("anthropic SDK not installed") from e

        from ..client import AnthropicClient
        AnthropicClient._load_env_from_claude_settings()

        # Strip empty-string env vars launchd inherits — see client.py
        # for the full story. Without this, the SDK happily sends
        # `x-api-key: ""` and 401s every call. This bit us twice because
        # AnthropicBackend has its own SDK constructor (separate from
        # AnthropicClient._client()) — both must do this dance.
        for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if os.environ.get(_k, None) == "":
                os.environ.pop(_k, None)

        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
        api_key    = os.environ.get("ANTHROPIC_API_KEY") or ""
        if not auth_token and not api_key:
            raise RuntimeError(
                "no API key — set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY "
                "(or log into Claude Code so its Keychain entry is readable)"
            )

        kwargs: dict[str, Any] = {}
        if auth_token:
            # OAuth bearer path — Claude Code Keychain token, OpenRouter, vLLM.
            # The `anthropic-beta: oauth-2025-04-20` header is required for
            # api.anthropic.com to accept bearer auth on /v1/messages.
            kwargs["auth_token"] = auth_token
            kwargs["default_headers"] = {
                "anthropic-beta": "oauth-2025-04-20",
            }
        else:
            kwargs["api_key"] = api_key
        if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
            kwargs["base_url"] = base_url
        self._sdk = anthropic.Anthropic(**kwargs)
        return self._sdk

    # ── core call ────────────────────────────────────────────────────────
    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        json_schema: Any | None,
    ) -> BackendResponse:
        sdk = self._client()
        # The Anthropic SDK does not consume json_schema directly; the
        # router enforces shape after the fact via validate_schema.
        response = sdk.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        raw_text = ""
        for block in (response.content or []):
            if getattr(block, "type", None) == "text":
                raw_text += getattr(block, "text", "")
        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0

        return BackendResponse(
            raw_text=raw_text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=model,
        )
