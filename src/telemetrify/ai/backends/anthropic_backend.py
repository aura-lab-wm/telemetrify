"""AnthropicBackend — wraps the official anthropic SDK.

Lazy SDK construction so import-time has no side effects. `is_available()`
returns True iff an API key is present in env (or ~/.claude/settings.json).
"""
from __future__ import annotations

import os
from typing import Any

from .base import BackendResponse


def _settings_json_env_value(name: str) -> str | None:
    """Best-effort peek at ~/.claude/settings.json's `env` block, WITHOUT
    the side effect of AnthropicClient._load_env_from_claude_settings()
    (which also mutates os.environ and may fall back to the macOS Keychain
    in the same call). Used only to tell an explicitly-configured
    ANTHROPIC_AUTH_TOKEN apart from one the Keychain fallback is about to
    fill in silently — see the BUG 1 comment in _client(). Read-only, never
    raises."""
    try:
        import json as _json
        from pathlib import Path
        path = Path.home() / ".claude" / "settings.json"
        if not path.exists():
            return None
        data = _json.loads(path.read_text())
        env = (data or {}).get("env") or {}
        v = env.get(name)
        return v if v else None
    except Exception:
        return None


def _is_official_anthropic_host(base_url: str) -> bool:
    """True iff base_url's host is exactly api.anthropic.com."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        host = (parsed.hostname or "").lower()
    except Exception:
        return False
    return host == "api.anthropic.com"


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

        # BUG 1 guard (2026-07 audit): AnthropicClient._load_env_from_claude_
        # settings() ends with a macOS-Keychain fallback that fills in
        # ANTHROPIC_AUTH_TOKEN whenever it's unset — with NO awareness of
        # ANTHROPIC_BASE_URL or an explicitly-configured ANTHROPIC_API_KEY.
        # That's correct for the default api.anthropic.com host, but wrong
        # the instant base_url points at a third-party endpoint (OpenRouter,
        # a self-hosted proxy): it would silently send this Mac's Claude
        # Code Keychain OAuth bearer token to that host, steamrolling
        # whatever api_key the user actually configured for it — a
        # credential leak with no host allow-list. We can't edit that
        # shared helper here (client.py's copy of this same bug is owned by
        # a parallel fix), so snapshot whether ANTHROPIC_AUTH_TOKEN was
        # ALREADY explicit (shell env or settings.json) *before* calling it
        # — that's the only way to tell "user configured this" apart from
        # "the Keychain fallback just filled it in", since both look
        # identical in os.environ afterward.
        explicit_auth_token = bool((os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip())
        if not explicit_auth_token:
            explicit_auth_token = bool(_settings_json_env_value("ANTHROPIC_AUTH_TOKEN"))

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
        base_url   = os.environ.get("ANTHROPIC_BASE_URL") or ""

        # api_key/base_url are never touched by the Keychain fallback (only
        # ANTHROPIC_AUTH_TOKEN is) — their presence here is always genuinely
        # explicit (shell env or settings.json), so no snapshot needed.
        keychain_sourced_token = bool(auth_token) and not explicit_auth_token
        host_is_official = not base_url or _is_official_anthropic_host(base_url)

        if keychain_sourced_token and (bool(api_key) or not host_is_official):
            # Never let an auto-hydrated Keychain token override an
            # explicitly-configured api_key, and never forward it to a
            # non-official host at all (no allow-list match). Drop it and
            # fall back to api_key (or fail loudly below if there isn't one)
            # instead of silently leaking it to an arbitrary base_url.
            auth_token = ""

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
        timeout: float | None = None,
    ) -> BackendResponse:
        sdk = self._client()
        # The Anthropic SDK does not consume json_schema directly; the
        # router enforces shape after the fact via validate_schema.
        kwargs: dict[str, Any] = {}
        if timeout:
            # Per-request override; without it the SDK default is ~600s, which
            # let the inline Stop-hook grade block for minutes.
            kwargs["timeout"] = timeout
        response = sdk.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **kwargs,
        )

        raw_text = ""
        for block in (response.content or []):
            if getattr(block, "type", None) == "text":
                raw_text += getattr(block, "text", "")
        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        # "end_turn" | "max_tokens" | "stop_sequence" | "tool_use" — see
        # BUG 4: surfaced so a max_tokens truncation isn't silently lost.
        stop_reason = getattr(response, "stop_reason", None)

        return BackendResponse(
            raw_text=raw_text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=model,
            stop_reason=stop_reason,
        )
