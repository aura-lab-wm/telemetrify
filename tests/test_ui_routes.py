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


# ─── BUG 1: push endpoint SSRF allowlist ───────────────────────────────

def test_push_subscribe_rejects_non_allowlisted_endpoint():
    """A caller-supplied endpoint on an arbitrary host must be rejected —
    otherwise this becomes an SSRF fan-out primitive (see push_notify.py)."""
    r = _client().post("/api/push/subscribe", json={
        "subscription": {
            "endpoint": "https://evil.example.com/collect",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
    })
    assert r.status_code == 400


def test_push_subscribe_rejects_non_https_allowlisted_host():
    """Even an allowlisted host must be rejected over plain http://."""
    r = _client().post("/api/push/subscribe", json={
        "subscription": {
            "endpoint": "http://fcm.googleapis.com/fcm/send/abc",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
    })
    assert r.status_code == 400


def test_push_subscribe_rejects_lookalike_host():
    """A hostname that merely contains an allowlisted domain as a substring
    (not a real subdomain) must still be rejected."""
    r = _client().post("/api/push/subscribe", json={
        "subscription": {
            "endpoint": "https://fcm.googleapis.com.evil.example.com/x",
            "keys": {"p256dh": "abc", "auth": "def"},
        },
    })
    assert r.status_code == 400


# ─── BUG 3: malformed-but-JSON-valid bodies must 400, not 500 ──────────

def test_push_subscribe_non_dict_keys_is_400_not_500():
    r = _client().post("/api/push/subscribe", json={
        "subscription": {
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "keys": "not-an-object",
        },
    })
    assert r.status_code == 400


def test_classify_ports_non_dict_list_item_is_400_not_500():
    r = _client().post("/api/classify-ports", json={"ports": ["just-a-string"]})
    assert r.status_code == 400


def test_classify_ports_non_dict_body_is_400_not_500():
    r = _client().post("/api/classify-ports", content="[1, 2, 3]",
                        headers={"content-type": "application/json"})
    assert r.status_code == 400


# ─── BUG 1(b)/(d): CSRF Origin/Referer check on state-changing routes ──

def test_cross_origin_post_is_rejected():
    r = _client().post(
        "/api/push/subscribe",
        json={},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_cross_origin_referer_is_rejected():
    r = _client().post(
        "/api/rerun/1",
        headers={"Referer": "https://evil.example.com/exploit.html"},
    )
    assert r.status_code == 403


def test_same_origin_post_is_allowed_through_to_normal_validation():
    """A same-origin Origin header must NOT itself trigger the 403 — the
    request should reach normal route validation (400 here, for a missing
    endpoint) instead."""
    r = _client().post(
        "/api/push/subscribe",
        json={},
        headers={"Origin": "http://localhost:8767"},
    )
    assert r.status_code == 400


def test_no_origin_or_referer_is_allowed_through():
    """Non-browser callers (curl, TestClient, this test suite) send neither
    header and must not be blocked by the CSRF guard."""
    r = _client().post("/api/push/unsubscribe", json={})
    assert r.status_code == 400


# ─── BUG 2: /sessions/{id} must 404 on an unknown session ──────────────

def test_unknown_session_is_404():
    r = _client().get("/sessions/does-not-exist-12345")
    assert r.status_code == 404


# ─── BUG 4: rerun budget cap + queue length cap ────────────────────────

def test_rerun_budget_over_cap_is_400():
    r = _client().post("/api/rerun/1", data={"budget_usd": "999"})
    assert r.status_code == 400


def test_queue_rerun_all_over_cap_is_400():
    # httpx's `data=` wants repeated-key form fields as {key: [values...]},
    # not a list of (key, value) tuples (which it silently mis-encodes).
    form = {"turn_ids": [str(i) for i in range(1, 60)]}
    r = _client().post("/api/queue/rerun-all", data=form)
    assert r.status_code == 400
