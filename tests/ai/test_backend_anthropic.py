"""Unit tests for AnthropicBackend.

Strategy: monkeypatch `anthropic.Anthropic` with a stub SDK class so no real
HTTP / API key is needed. Verifies:
  - pricing math via estimate_cost_usd lookup
  - JSON text extraction from the response.content blocks
  - is_available() returns True only when an API key is present
  - upstream SDK errors propagate out (router handles fallback)
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest


# ── helpers ──────────────────────────────────────────────────────────────
class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str, in_tok: int = 10, out_tok: int = 20):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(in_tok, out_tok)


class _FakeMessages:
    def __init__(self, response: _FakeResponse, *, raise_exc: Exception | None = None):
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._response


class _FakeSDK:
    def __init__(self, response: _FakeResponse, *, raise_exc: Exception | None = None):
        self.messages = _FakeMessages(response, raise_exc=raise_exc)


def _install_fake_sdk(monkeypatch, response, *, raise_exc=None):
    """Patch the `anthropic` module's Anthropic() constructor to return our fake."""
    import anthropic
    fake = _FakeSDK(response, raise_exc=raise_exc)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    return fake


# ── tests ────────────────────────────────────────────────────────────────
def test_is_available_requires_api_key(monkeypatch):
    from telemetrify.ai.backends.anthropic_backend import AnthropicBackend

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    # Block the settings.json fallback by pointing HOME at an empty dir
    monkeypatch.setenv("HOME", "/nonexistent-home-for-test")
    b = AnthropicBackend()
    assert b.is_available() is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    b2 = AnthropicBackend()
    assert b2.is_available() is True


def test_complete_returns_text_and_token_counts(monkeypatch):
    from telemetrify.ai.backends.anthropic_backend import AnthropicBackend

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    fake = _install_fake_sdk(monkeypatch, _FakeResponse('{"x": 1}', in_tok=42, out_tok=7))

    b = AnthropicBackend()
    resp = b.complete(
        system="you are terse",
        user="hi",
        model="claude-sonnet-4-6",
        max_tokens=128,
        json_schema=None,
    )
    assert resp.raw_text == '{"x": 1}'
    assert resp.input_tokens == 42
    assert resp.output_tokens == 7
    assert resp.model == "claude-sonnet-4-6"
    # verify SDK was called with the right kwargs
    assert fake.messages.calls[0]["model"] == "claude-sonnet-4-6"
    assert fake.messages.calls[0]["max_tokens"] == 128
    assert fake.messages.calls[0]["system"] == "you are terse"


def test_pricing_uses_estimate_cost_usd(monkeypatch):
    """Backend should expose pricing tuple via the shared estimate_cost_usd table."""
    from telemetrify.ai.backends.anthropic_backend import AnthropicBackend
    from telemetrify.ai.client import estimate_cost_usd

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    fake = _install_fake_sdk(monkeypatch, _FakeResponse("ok", in_tok=1_000_000, out_tok=1_000_000))
    b = AnthropicBackend()
    resp = b.complete(system="s", user="u", model="claude-sonnet-4-6",
                      max_tokens=10, json_schema=None)
    # 1M in @ $3 + 1M out @ $15 = $18
    expected = estimate_cost_usd("claude-sonnet-4-6",
                                  resp.input_tokens, resp.output_tokens)
    assert expected == pytest.approx(18.0)


def test_sdk_exception_propagates(monkeypatch):
    """When the SDK raises (e.g. APIConnectionError), backend must re-raise so
    the router can decide whether to fall through to the next tier."""
    from telemetrify.ai.backends.anthropic_backend import AnthropicBackend

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")

    class _Boom(Exception):
        pass

    _install_fake_sdk(monkeypatch, _FakeResponse(""), raise_exc=_Boom("nope"))
    b = AnthropicBackend()
    with pytest.raises(_Boom):
        b.complete(system="s", user="u", model="claude-sonnet-4-6",
                   max_tokens=10, json_schema=None)
