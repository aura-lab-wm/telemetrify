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


def test_out_of_range_min_tokens_is_dropped_not_overflow():
    """A min_tokens value outside SQLite's signed-64-bit range used to reach
    `conn.execute(...)` and raise an unhandled OverflowError (HTTP 500). It
    should instead be silently dropped, same as any other unparseable value."""
    f = parse_filters({"min_tokens": "99999999999999999999999999"})
    assert "input_tokens" not in f.where
    assert f.params == []


def test_out_of_range_negative_max_latency_is_dropped():
    f = parse_filters({"max_latency_ms": "-99999999999999999999999999"})
    assert "latency_ms" not in f.where
    assert f.params == []


def test_in_range_tokens_still_bind_normally():
    f = parse_filters({"min_tokens": "100", "max_tokens": "200"})
    assert f.params == [100, 200]


def test_boundary_int64_values_are_accepted():
    max64 = 2**63 - 1
    min64 = -(2**63)
    f = parse_filters({"min_latency_ms": str(min64), "max_latency_ms": str(max64)})
    assert f.params == [min64, max64]


def test_out_of_range_cluster_is_dropped():
    f = parse_filters({"cluster": str(2**64)})
    assert "turn_cluster" not in f.where
    assert f.params == []


def test_cwd_glob_escapes_literal_percent():
    """A literal `%` in the cwd value must match literally, not act as an
    SQL LIKE wildcard that matches every row."""
    f = parse_filters({"cwd_glob": "/Users/x/100%done"})
    assert "ESCAPE" in f.where
    assert f.params == ["/Users/x/100\\%done"]


def test_cwd_glob_escapes_literal_underscore():
    f = parse_filters({"cwd_glob": "/Users/x/a_b"})
    assert f.params == ["/Users/x/a\\_b"]


def test_cwd_glob_star_is_still_the_app_wildcard():
    f = parse_filters({"cwd_glob": "/Users/x/*"})
    assert f.params == ["/Users/x/%"]


def test_cwd_glob_mixed_literal_and_wildcard():
    f = parse_filters({"cwd_glob": "/Users/x/100%/*"})
    assert f.params == ["/Users/x/100\\%/%"]


def test_cwd_glob_percent_does_not_match_every_row(migrated_db):
    """End-to-end: a cwd containing a literal '%' must not act as a
    wildcard matching every row in the table."""
    conn = migrated_db
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO turns (id, session_id, user_text, assistant_text, started_at, origin, cwd) "
        "VALUES (1, 's1', 'x', 'a', '2026-05-25T10:00:00Z', 'organic', '/Users/x/100%done')"
    )
    conn.execute(
        "INSERT INTO turns (id, session_id, user_text, assistant_text, started_at, origin, cwd) "
        "VALUES (2, 's1', 'x', 'a', '2026-05-25T10:00:00Z', 'organic', '/Users/x/other')"
    )
    conn.commit()

    f = parse_filters({"cwd_glob": "/Users/x/100%done"})
    rows = conn.execute(
        f"SELECT t.id FROM turns t WHERE {f.where} ORDER BY t.id", f.params
    ).fetchall()
    assert [r[0] for r in rows] == [1]


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
