"""Regression for hybrid_search applying filters AFTER candidate retrieval.

Before the fix, the vec/FTS sides pulled their top-`DEFAULT_FANOUT` (50) purely
by similarity and the filter was applied only after fusion — so a selective
filter whose match wasn't already in the unfiltered top-50 returned EMPTY even
though a matching turn existed. The fix applies the filter inside the candidate
CTEs and deepens the pool (FILTERED_FANOUT) when filtering.

We seed 55 "decoy" turns whose embeddings sit right next to the query vector and
1 "target" turn (unique model) whose embedding is orthogonal/far. Filtering by
the target's model must still find it, even though it ranks far below the top-50
by similarity.
"""
from __future__ import annotations

import pytest

from telemetrify.db import EMBEDDING_DIM


def _vec(primary_idx: int, noise: float = 0.0) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[primary_idx] = 1.0
    if primary_idx != 1:
        v[1] = noise
    return v


def _mk_turn(uuid: str, model: str):
    from telemetrify.transcript import Turn
    return Turn(
        session_id="s1", user_uuid=uuid, parent_uuid=None, prompt_id=None,
        user_text="alpha beta", assistant_text="gamma delta", thinking_text="",
        model=model, started_at="2026-06-03T00:00:00.000Z",
        finished_at="2026-06-03T00:00:01.000Z", latency_ms=10,
        input_tokens=1, output_tokens=1, cache_creation_tokens=0, cache_read_tokens=0,
        cwd="/x", git_branch=None, project_dir=None, transcript_path=None,
        cli_version=None, entrypoint=None, user_type=None,
        attribution_skill=None, attribution_plugin=None,
        tool_call_count=0, assistant_message_count=1, tool_calls=[], raw_json="",
    )


def test_filtered_match_beyond_default_fanout_is_found(migrated_db, monkeypatch):
    from telemetrify import search
    from telemetrify.store import upsert_session, insert_turn

    conn = migrated_db
    # Query vector points at index 0; decoys sit basically on top of it.
    monkeypatch.setattr(search, "embed", lambda q: _vec(0))

    def add(uuid, model, emb):
        t = _mk_turn(uuid, model)
        upsert_session(conn, t)
        return insert_turn(conn, t, emb)

    for i in range(55):
        add(f"decoy-{i}", "decoy-model", _vec(0, noise=0.001 * (i + 1)))
    target_id = add("target-1", "target-model", _vec(EMBEDDING_DIM - 1))  # orthogonal → far

    # FTS token matches nothing, so only the vec side contributes candidates.
    res = search.hybrid_search(
        conn, "zzqqnomatchtoken", k=10,
        filters=search.parse_filters({"model": "target-model"}),
    )
    ids = [r["id"] for r in res]
    assert target_id in ids, (
        "a filtered match ranked beyond the default fanout must still be "
        "returned (filters now applied inside the deepened candidate pool)"
    )
    # only the target has that model
    assert all(r["model"] == "target-model" for r in res)


def test_similar_turns_not_starved_by_same_session(migrated_db, monkeypatch):
    """similar_turns excludes self + same-session turns. If the KNN pool is too
    shallow, a tight cluster of same-session neighbors crowds out every
    cross-session match and the function returns far fewer than k (or zero).
    The pool must be deep enough to still surface k other-session turns."""
    from telemetrify import search
    from telemetrify.store import upsert_session, insert_turn
    from telemetrify.transcript import Turn

    conn = migrated_db

    def add(uuid, session, emb):
        t = _mk_turn(uuid, "m")
        t = Turn(**{**t.__dict__, "session_id": session, "user_uuid": uuid})
        upsert_session(conn, t)
        return insert_turn(conn, t, emb)

    # target + 10 SAME-session neighbors sit closest to the query point;
    # 5 OTHER-session turns sit slightly further out.
    target = add("target", "S", _vec(0, noise=0.0))
    for i in range(10):
        add(f"same-{i}", "S", _vec(0, noise=0.0001 * (i + 1)))
    for i in range(5):
        add(f"other-{i}", "O", _vec(0, noise=0.01 * (i + 1)))

    res = search.similar_turns(conn, target, k=5)
    assert len(res) == 5, f"expected 5 cross-session matches, got {len(res)}"
    assert all(r["session_id"] == "O" for r in res)
    assert all(r["id"] != target for r in res)


def test_unfiltered_search_returns_nearest(migrated_db, monkeypatch):
    """Sanity: without a filter, the nearest decoys come back (the fix must not
    regress the common path)."""
    from telemetrify import search
    from telemetrify.store import upsert_session, insert_turn

    conn = migrated_db
    monkeypatch.setattr(search, "embed", lambda q: _vec(0))

    def add(uuid, model, emb):
        t = _mk_turn(uuid, model)
        upsert_session(conn, t)
        return insert_turn(conn, t, emb)

    near = add("near", "m", _vec(0, noise=0.0001))
    add("far", "m", _vec(EMBEDDING_DIM - 1))

    res = search.hybrid_search(conn, "zzqqnomatchtoken", k=5)
    assert res and res[0]["id"] == near
