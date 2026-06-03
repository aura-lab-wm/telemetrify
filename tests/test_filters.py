"""Tests for the search filter-bar parser, focused on the `tag` filter
(annotation-tag membership) added for curated workspaces like the seminar set.
"""
from __future__ import annotations

from telemetrify.search import ALLOWED_FILTERS, parse_filters


def test_tag_is_allowed():
    assert "tag" in ALLOWED_FILTERS


def test_tag_builds_annotation_exists_clause():
    f = parse_filters({"tag": "seminar-coding-agents"})
    assert "annotations" in f.where
    assert "LIKE" in f.where
    # whole-element, space-tolerant CSV match
    assert f.params == ["%,seminar-coding-agents,%"]


def test_tag_strips_spaces_for_csv_match():
    f = parse_filters({"tag": " seminar coding "})
    assert f.params == ["%,seminarcoding,%"]


def test_blank_tag_is_ignored():
    f = parse_filters({"tag": ""})
    assert "annotations" not in f.where
    assert f.params == []


def test_cluster_zero_is_not_dropped():
    """cluster_id 0 is a real cluster, but the walrus `if (v := ...)` treated
    it as falsy and silently dropped the filter — so a cluster-0 workspace saw
    the whole corpus instead of its cluster."""
    f = parse_filters({"cluster": "0"})
    assert "turn_cluster" in f.where
    assert f.params == [0]


def test_cluster_nonzero_still_works():
    f = parse_filters({"cluster": "7"})
    assert "turn_cluster" in f.where
    assert f.params == [7]


def test_cluster_blank_ignored():
    f = parse_filters({"cluster": ""})
    assert "turn_cluster" not in f.where


def test_tag_combines_with_other_filters():
    f = parse_filters({"tag": "seminar-coding-agents", "model": "claude-opus-4-7"})
    assert "t.model = ?" in f.where
    assert "annotations" in f.where
    assert "claude-opus-4-7" in f.params
    assert "%,seminar-coding-agents,%" in f.params


def test_tag_filters_real_rows(migrated_db):
    """End-to-end: only annotation-tagged turns survive the generated WHERE."""
    conn = migrated_db
    conn.execute("PRAGMA foreign_keys=OFF")  # FK off → no parent session row needed
    for tid, txt in [(1, "kept"), (2, "dropped"), (3, "kept-too")]:
        conn.execute(
            "INSERT INTO turns (id, session_id, user_text, assistant_text, started_at, origin) "
            "VALUES (?, 's1', ?, 'a', '2026-05-25T10:00:00Z', 'organic')",
            (tid, txt),
        )
    conn.execute("INSERT INTO annotations (turn_id, tags) VALUES (1, 'seminar-coding-agents')")
    conn.execute("INSERT INTO annotations (turn_id, tags) VALUES (2, 'phase3,roundtrip')")
    conn.execute("INSERT INTO annotations (turn_id, tags) VALUES (3, 'foo, seminar-coding-agents, bar')")
    conn.commit()

    f = parse_filters({"tag": "seminar-coding-agents"})
    rows = conn.execute(
        f"SELECT t.id FROM turns t WHERE {f.where} ORDER BY t.id", f.params
    ).fetchall()
    assert [r[0] for r in rows] == [1, 3]
