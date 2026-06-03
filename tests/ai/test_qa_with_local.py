"""End-to-end: /api/ask delivers SSE events using a fake local backend.

Wires a FakeBackend through `default_router` so the planner + synthesizer
both route to local without touching Anthropic. Verifies the SSE stream
emits the expected `plan` / `sources` / `delta` / `done` sequence and that
the delta text came from the local backend.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient


class _LocalBackend:
    """Returns a structured JSON plan for the planner, free-form markdown for
    the synthesizer (planner has the JSON keys the schema requires, second
    call returns prose)."""
    name = "rocco"

    def __init__(self):
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def complete(self, *, system, user, model, max_tokens, json_schema, timeout=None):
        from telemetrify.ai.backends.base import BackendResponse
        self.calls += 1
        if json_schema is not None or "QUESTION:" in user and "SOURCES" not in user:
            # planner stage
            txt = json.dumps({
                "semantic_query": "test query",
                "filters": {},
                "intent": "find",
                "k": 5,
            })
        else:
            # synthesizer stage — free-form markdown
            txt = "Answer from **rocco** local backend. [#1]"
        return BackendResponse(raw_text=txt, input_tokens=10,
                                output_tokens=12, model=model)


@pytest.fixture
def client_with_local_backend(monkeypatch, migrated_db: sqlite3.Connection):
    """Wire a TestClient where /api/ask's qa pipeline routes to a local fake."""
    import telemetrify.ui.app as app_mod
    from telemetrify.ai import router as router_mod

    monkeypatch.setattr(app_mod, "connect", lambda: migrated_db)

    fake = _LocalBackend()

    def fake_default_router(conn, override_budget_usd=None):
        return router_mod.BackendRouter(
            conn=conn, backends=[fake], default_order=["rocco"],
            override_budget_usd=override_budget_usd,
        )
    monkeypatch.setattr(router_mod, "default_router", fake_default_router)

    # Stub hybrid_search so the synthesizer always has at least one source.
    from telemetrify.ai import qa as qa_mod

    def fake_hybrid_search(conn, q, k=20, filters=None):
        return [{
            "id": 1, "session_id": "s1",
            "started_at": "2026-01-01T00:00:00",
            "cwd": "/tmp", "model": "claude-opus-4-7",
            "user_text": "how do I test pulse?",
            "assistant_text": "use pytest",
        }]
    monkeypatch.setattr(qa_mod, "hybrid_search", fake_hybrid_search)

    return TestClient(app_mod.app), fake


def test_api_ask_streams_from_local_backend(client_with_local_backend):
    client, fake = client_with_local_backend

    resp = client.post("/api/ask", json={"question": "how do I test pulse?"})
    assert resp.status_code == 200

    # parse SSE — each event is "data: {json}\n\n"
    events = []
    for chunk in resp.text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data:"):
            continue
        payload = chunk[len("data:"):].strip()
        events.append(json.loads(payload))

    event_names = [e["event"] for e in events]
    assert "plan" in event_names, f"events={events!r}"
    assert "sources" in event_names
    assert "delta" in event_names
    assert "done" in event_names

    # the delta must have come from the local backend
    deltas = [e["data"] for e in events if e["event"] == "delta"]
    assert any("rocco" in d for d in deltas), f"deltas={deltas!r}"

    # local backend was called at least twice (planner + synthesizer)
    assert fake.calls >= 2
