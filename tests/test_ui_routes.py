"""Malformed input to write/POST routes must yield a clean 4xx, not a 500.

These exercise only the validation/early-return paths (missing fields), which
return BEFORE any DB access, so no fixture DB is needed.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    import telemetrify.ui.app as app_mod
    return TestClient(app_mod.app)


def test_api_ask_missing_question_is_400():
    r = _client().post("/api/ask", json={})
    assert r.status_code == 400


def test_api_ask_malformed_body_is_4xx_not_500():
    r = _client().post("/api/ask", content="not json",
                       headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500


def test_push_subscribe_missing_endpoint_is_400():
    r = _client().post("/api/push/subscribe", json={})
    assert r.status_code == 400


def test_push_unsubscribe_missing_endpoint_is_400():
    r = _client().post("/api/push/unsubscribe", json={})
    assert r.status_code == 400
