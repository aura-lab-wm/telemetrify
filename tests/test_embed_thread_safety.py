"""Regression: embed.py's lazy model loader used to be a bare
@lru_cache(maxsize=1), which does not guard the first-load path against
concurrent callers -- two threads can both see an empty cache and both
construct a SentenceTransformer. Verify the double-checked-locking
replacement lets many concurrent first-callers race without ever
constructing more than one model instance, and that they all end up
sharing that single instance.
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest


@pytest.fixture(autouse=True)
def _reset_model_singleton(monkeypatch):
    """Every test gets a clean, unloaded singleton regardless of what ran
    before it (or what the real sentence-transformers model would have
    left behind)."""
    from telemetrify import embed
    monkeypatch.setattr(embed, "_model_instance", None)
    yield


def test_concurrent_first_callers_construct_model_exactly_once(monkeypatch):
    from telemetrify import embed

    construct_count = 0
    count_lock = threading.Lock()

    class _FakeModel:
        def __init__(self, name):
            nonlocal construct_count
            with count_lock:
                construct_count += 1
            # Widen the race window: a caller that isn't properly blocked on
            # the load lock would have plenty of time here to also see an
            # empty cache and start its own (redundant) construction.
            time.sleep(0.05)

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    results: list = []
    results_lock = threading.Lock()

    def worker():
        m = embed._model()
        with results_lock:
            results.append(m)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert construct_count == 1, (
        f"expected exactly one SentenceTransformer construction across 16 "
        f"racing first-callers, got {construct_count}"
    )
    assert len(results) == 16
    assert all(r is results[0] for r in results), "every caller must share the one instance"


def test_model_is_cached_across_sequential_calls(monkeypatch):
    from telemetrify import embed

    construct_count = 0

    class _FakeModel:
        def __init__(self, name):
            nonlocal construct_count
            construct_count += 1

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    first = embed._model()
    second = embed._model()
    assert first is second
    assert construct_count == 1
