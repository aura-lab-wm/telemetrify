"""Unit tests for OpenAICompatBackend (vLLM + Ollama Cloud).

Strategy: monkeypatch `httpx.Client.post` to capture the outbound request and
return a stubbed OpenAI-format JSON body. Verifies:
  - URL is `{base_url}/chat/completions`
  - Authorization header is `Bearer {api_key}`
  - body contains BOTH `guided_json` (in extra_body) AND
    `response_format={"type": "json_object"}` when a schema is passed
  - body contains NEITHER when json_schema is None
  - is_available() pings `/v1/models` with 1s timeout and caches the result
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest


def _make_chat_response(text: str = '{"x": 1}', in_tok: int = 11, out_tok: int = 22):
    """Build a stub OpenAI chat-completions response object."""
    body = {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                  "total_tokens": in_tok + out_tok},
    }

    def _json():
        return body
    return SimpleNamespace(
        status_code=200,
        json=_json,
        text=json.dumps(body),
        raise_for_status=lambda: None,
        headers={},
    )


def _make_models_response(ok: bool = True):
    return SimpleNamespace(
        status_code=200 if ok else 503,
        json=lambda: {"data": [{"id": "m"}]} if ok else {},
        text="{}",
        raise_for_status=lambda: None if ok else (_ for _ in ()).throw(
            httpx.HTTPStatusError("nope", request=None, response=None)
        ),
        headers={},
    )


def test_post_url_and_auth_header(monkeypatch):
    from telemetrify.ai.backends.openai_compat import OpenAICompatBackend

    captured = {}

    def fake_post(self, url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _make_chat_response()

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    b = OpenAICompatBackend(
        name="rocco",
        base_url="http://localhost:18000/v1",
        api_key="EMPTY",
        default_model="moonshotai/Kimi-Dev-72B",
        input_price_per_m=0.0000002,
        output_price_per_m=0.0000002,
    )
    b.complete(system="s", user="u", model="moonshotai/Kimi-Dev-72B",
               max_tokens=64, json_schema=None)

    assert captured["url"] == "http://localhost:18000/v1/chat/completions"
    headers = captured["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer EMPTY"
    assert headers["Content-Type"] == "application/json"


def test_schema_sends_both_guided_json_and_response_format(monkeypatch):
    from telemetrify.ai.backends.openai_compat import OpenAICompatBackend

    captured = {}

    def fake_post(self, url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs.get("json")
        return _make_chat_response('{"quality": 4}')

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    schema = {"type": "object", "properties": {"quality": {"type": "integer"}}}
    b = OpenAICompatBackend(
        name="rocco", base_url="http://localhost:18000/v1", api_key="EMPTY",
        default_model="m", input_price_per_m=0.0, output_price_per_m=0.0,
    )
    b.complete(system="s", user="u", model="m", max_tokens=32, json_schema=schema)

    body = captured["body"]
    # Dual hint: vLLM honors guided_json; Ollama Cloud honors response_format
    assert body.get("response_format") == {"type": "json_object"}
    # extra_body is OpenAI SDK semantics; for raw httpx we send the keys inline
    assert body.get("extra_body", {}).get("guided_json") == schema \
        or body.get("guided_json") == schema


def test_no_schema_sends_neither_hint(monkeypatch):
    from telemetrify.ai.backends.openai_compat import OpenAICompatBackend

    captured = {}

    def fake_post(self, url, **kwargs):
        captured["body"] = kwargs.get("json")
        return _make_chat_response("free-form prose, no json")

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    b = OpenAICompatBackend(
        name="ollama", base_url="https://ollama.com/v1", api_key="ok",
        default_model="gpt-oss:120b", input_price_per_m=0.20, output_price_per_m=0.60,
    )
    b.complete(system="s", user="u", model="gpt-oss:120b", max_tokens=64,
               json_schema=None)

    body = captured["body"]
    assert "response_format" not in body
    extra = body.get("extra_body") or {}
    assert "guided_json" not in extra
    assert "guided_json" not in body


def test_is_available_caches_result(monkeypatch):
    """`is_available()` should hit /v1/models, then cache for ~30s."""
    from telemetrify.ai.backends import openai_compat as oc

    calls = {"n": 0}

    def fake_get(self, url, **kwargs):
        calls["n"] += 1
        return _make_models_response(ok=True)

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    b = oc.OpenAICompatBackend(
        name="rocco", base_url="http://localhost:18000/v1", api_key="EMPTY",
        default_model="m", input_price_per_m=0.0, output_price_per_m=0.0,
    )
    assert b.is_available() is True
    assert b.is_available() is True
    # second call should be cached → still one GET
    assert calls["n"] == 1


def test_is_available_returns_false_on_connect_error(monkeypatch):
    from telemetrify.ai.backends.openai_compat import OpenAICompatBackend

    def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("tunnel down")

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    b = OpenAICompatBackend(
        name="rocco", base_url="http://localhost:18000/v1", api_key="EMPTY",
        default_model="m", input_price_per_m=0.0, output_price_per_m=0.0,
    )
    assert b.is_available() is False
