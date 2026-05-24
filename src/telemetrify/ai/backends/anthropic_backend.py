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
        # Hydrate from settings.json if the shell env is empty.
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
        api_key = (
            os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        )
        if not api_key:
            raise RuntimeError(
                "no API key — set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY "
                "(or populate the `env` block in ~/.claude/settings.json)"
            )
        base_url = os.environ.get("ANTHROPIC_BASE_URL") or None
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
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
