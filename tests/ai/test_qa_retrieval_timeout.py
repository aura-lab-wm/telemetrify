"""Regression for the /api/ask hang: hybrid_search()/embed() had no wall-clock
bound, so a wedged embed-model load (or any other stuck retrieval call) could
hang the SSE request forever, permanently consuming an anyio thread-pool slot
(see embed.py's _model() double-checked-locking fix for the load-time race
this guards against).

`_retrieve_with_timeout` must:
  1. actually give up waiting once `timeout` elapses, even if the underlying
     hybrid_search() call never returns (simulated with a long real sleep);
  2. let `stream_answer` degrade that timeout to a clean SSE {"event":"error"}
     instead of hanging or raising out of the generator.
"""
from __future__ import annotations

import time

import pytest


def test_retrieve_with_timeout_gives_up_on_a_hung_search(migrated_db, monkeypatch):
    """A hybrid_search() that never returns must not block the caller past
    the configured timeout."""
    from telemetrify.ai import qa as qa_mod

    def hung_hybrid_search(conn, q, k=20, filters=None):
        time.sleep(5.0)  # much longer than the timeout below
        raise AssertionError("should never actually finish")

    monkeypatch.setattr(qa_mod, "hybrid_search", hung_hybrid_search)

    from telemetrify.search import Filters

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        qa_mod._retrieve_with_timeout(
            migrated_db, "does it matter", k=5, filters=Filters(), timeout=0.5,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, (
        f"expected to give up around the 0.5s timeout, took {elapsed:.2f}s -- "
        "the caller is blocking on the hung worker instead of abandoning it"
    )


def test_stream_answer_degrades_retrieval_timeout_to_clean_sse_error(
    monkeypatch, migrated_db,
):
    """End-to-end through stream_answer(): a retrieval timeout must surface
    as a normal {"event": "error"} SSE item -- plan still arrives, but
    sources/delta/done never do, and the generator terminates promptly
    instead of hanging."""
    from telemetrify.ai import qa as qa_mod

    def fake_plan(conn, question, *, model_override=None, feature="qa"):
        return {"semantic_query": "q", "filters": {}, "intent": "find", "k": 5}

    monkeypatch.setattr(qa_mod, "plan", fake_plan)

    def hung_hybrid_search(conn, q, k=20, filters=None):
        time.sleep(5.0)
        raise AssertionError("should never actually finish")

    monkeypatch.setattr(qa_mod, "hybrid_search", hung_hybrid_search)
    monkeypatch.setattr(qa_mod, "RETRIEVAL_TIMEOUT_S", 0.5)

    started = time.monotonic()
    events = list(qa_mod.stream_answer(migrated_db, "how many sessions?"))
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"stream_answer hung instead of timing out ({elapsed:.2f}s)"
    names = [e["event"] for e in events]
    assert names == ["plan", "error"], f"events={events!r}"
    assert "timed out" in events[-1]["data"]


def test_stream_answer_synthesizer_generic_failure_degrades_to_sse_error(
    monkeypatch, migrated_db,
):
    """Regression: previously only BudgetExceeded was caught around the
    synthesizer call, so any other failure (rate limit, all-tiers-down,
    network error) propagated and crashed the SSE generator. It must now
    degrade to a clean error event, matching the planner's handling."""
    from telemetrify.ai import qa as qa_mod

    def fake_plan(conn, question, *, model_override=None, feature="qa"):
        return {"semantic_query": "q", "filters": {}, "intent": "find", "k": 5}

    monkeypatch.setattr(qa_mod, "plan", fake_plan)

    def fake_hybrid_search(conn, q, k=20, filters=None):
        return [{
            "id": 1, "session_id": "s1", "started_at": "2026-01-01T00:00:00",
            "cwd": "/tmp", "model": "m",
            "user_text": "q", "assistant_text": "a",
        }]

    monkeypatch.setattr(qa_mod, "hybrid_search", fake_hybrid_search)

    class _ExplodingClient:
        def __init__(self, conn):
            pass

        def call(self, **kwargs):
            raise RuntimeError("all tiers down")

    monkeypatch.setattr(qa_mod, "AnthropicClient", _ExplodingClient)

    events = list(qa_mod.stream_answer(migrated_db, "how many sessions?"))
    names = [e["event"] for e in events]
    assert names == ["plan", "sources", "error"], f"events={events!r}"
    assert "synthesizer failed" in events[-1]["data"]
