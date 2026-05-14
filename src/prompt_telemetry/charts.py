"""Pure-Python aggregation helpers for the analysis dashboard.

Each function takes a sqlite3.Connection and returns a JSON-serializable dict
shaped like a Plotly figure spec: {"data": [...traces...], "layout": {...}}.
SQL lives here so the route handlers stay trivial.

Conventions:
- `started_at` is ISO8601 with a trailing Z. SQLite's `date()`/`strftime()`
  handle it correctly when used as TEXT.
- We use Plotly's "plotly_dark" layout template for consistency with the UI.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

_DARK = {"template": "plotly_dark", "margin": {"l": 50, "r": 20, "t": 40, "b": 50}}


def _layout(title: str, **extra: Any) -> dict:
    layout = {"title": {"text": title}, **_DARK}
    layout.update(extra)
    return layout


def turns_per_day(conn: sqlite3.Connection) -> dict:
    """Line chart of turn counts per UTC day, last 90 days."""
    rows = conn.execute(
        """
        SELECT date(started_at) AS d, COUNT(*) AS n
        FROM turns
        WHERE started_at >= date('now', '-90 days')
        GROUP BY d
        ORDER BY d
        """
    ).fetchall()
    x = [r["d"] for r in rows]
    y = [r["n"] for r in rows]
    return {
        "data": [{
            "type": "scatter",
            "mode": "lines+markers",
            "x": x,
            "y": y,
            "name": "turns",
            "line": {"color": "#7aa2ff"},
        }],
        "layout": _layout("Turns per day (last 90d)",
                          xaxis={"title": "day"},
                          yaxis={"title": "turns"}),
    }


def tokens_by_model(conn: sqlite3.Connection) -> dict:
    """Stacked area chart, daily total tokens per model."""
    rows = conn.execute(
        """
        SELECT date(started_at) AS d,
               COALESCE(model, 'unknown') AS m,
               SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)) AS tok
        FROM turns
        WHERE started_at >= date('now', '-90 days')
        GROUP BY d, m
        ORDER BY d
        """
    ).fetchall()

    per_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    days: list[str] = []
    seen_days: set[str] = set()
    for r in rows:
        d, m, tok = r["d"], r["m"], r["tok"]
        per_model[m][d] = tok
        if d not in seen_days:
            seen_days.add(d)
            days.append(d)

    palette = ["#7aa2ff", "#b5e3a2", "#ffb56b", "#ff7676", "#c792ea",
               "#82d4dd", "#f6c177", "#94e2d5"]
    traces = []
    for i, (model, by_day) in enumerate(sorted(per_model.items())):
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "stackgroup": "one",
            "x": days,
            "y": [by_day.get(d, 0) for d in days],
            "name": model,
            "line": {"color": palette[i % len(palette)]},
        })
    return {
        "data": traces,
        "layout": _layout("Tokens by model (stacked, daily)",
                          xaxis={"title": "day"},
                          yaxis={"title": "tokens"}),
    }


def tool_heatmap(conn: sqlite3.Connection) -> dict:
    """Heatmap: tool_name (y) × ISO week (x), counts as cell intensity."""
    rows = conn.execute(
        """
        SELECT tc.tool_name AS tool,
               strftime('%Y-W%W', t.started_at) AS wk,
               COUNT(*) AS n
        FROM tool_calls tc JOIN turns t ON t.id = tc.turn_id
        WHERE t.started_at >= date('now', '-180 days')
        GROUP BY tool, wk
        ORDER BY wk, tool
        """
    ).fetchall()

    weeks: list[str] = []
    tools: list[str] = []
    seen_w: set[str] = set()
    seen_t: set[str] = set()
    cell: dict[tuple[str, str], int] = {}
    for r in rows:
        if r["wk"] not in seen_w:
            seen_w.add(r["wk"])
            weeks.append(r["wk"])
        if r["tool"] not in seen_t:
            seen_t.add(r["tool"])
            tools.append(r["tool"])
        cell[(r["tool"], r["wk"])] = r["n"]

    z = [[cell.get((tool, wk), 0) for wk in weeks] for tool in tools]
    return {
        "data": [{
            "type": "heatmap",
            "x": weeks,
            "y": tools,
            "z": z,
            "colorscale": "Viridis",
        }],
        "layout": _layout("Tool calls heatmap (tool × ISO week)",
                          xaxis={"title": "ISO week"},
                          yaxis={"title": "tool"}),
    }


def error_rate(conn: sqlite3.Connection) -> dict:
    """Weekly % of turns with ≥1 tool error."""
    rows = conn.execute(
        """
        SELECT strftime('%Y-W%W', t.started_at) AS wk,
               COUNT(DISTINCT t.id) AS turns,
               COUNT(DISTINCT CASE WHEN tc.is_error = 1 THEN t.id END) AS err_turns
        FROM turns t LEFT JOIN tool_calls tc ON tc.turn_id = t.id
        WHERE t.started_at >= date('now', '-180 days')
        GROUP BY wk
        ORDER BY wk
        """
    ).fetchall()
    x = [r["wk"] for r in rows]
    y = [(100.0 * r["err_turns"] / r["turns"]) if r["turns"] else 0.0 for r in rows]
    return {
        "data": [{
            "type": "scatter",
            "mode": "lines+markers",
            "x": x,
            "y": y,
            "name": "% with tool error",
            "line": {"color": "#ff7676"},
        }],
        "layout": _layout("Tool error rate (weekly)",
                          xaxis={"title": "ISO week"},
                          yaxis={"title": "% of turns", "ticksuffix": "%"}),
    }


def latency(conn: sqlite3.Connection) -> dict:
    """Weekly p50 / p95 latency in ms. Computed in Python from per-week samples."""
    rows = conn.execute(
        """
        SELECT strftime('%Y-W%W', started_at) AS wk, latency_ms
        FROM turns
        WHERE started_at >= date('now', '-180 days')
          AND latency_ms IS NOT NULL
        ORDER BY wk
        """
    ).fetchall()
    buckets: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        buckets[r["wk"]].append(int(r["latency_ms"]))

    weeks = sorted(buckets)

    def _pct(samples: list[int], p: float) -> float:
        if not samples:
            return 0.0
        s = sorted(samples)
        k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
        return float(s[k])

    p50 = [_pct(buckets[w], 50) for w in weeks]
    p95 = [_pct(buckets[w], 95) for w in weeks]
    return {
        "data": [
            {"type": "scatter", "mode": "lines+markers", "x": weeks, "y": p50,
             "name": "p50", "line": {"color": "#7aa2ff"}},
            {"type": "scatter", "mode": "lines+markers", "x": weeks, "y": p95,
             "name": "p95", "line": {"color": "#ffb56b"}},
        ],
        "layout": _layout("Latency (weekly p50/p95)",
                          xaxis={"title": "ISO week"},
                          yaxis={"title": "ms"}),
    }


def annotations(conn: sqlite3.Connection) -> dict:
    """Pie chart of annotation rating buckets (-1 / 0 / +1)."""
    rows = conn.execute(
        "SELECT rating, COUNT(*) AS n FROM annotations GROUP BY rating"
    ).fetchall()
    labels_by_rating = {-1: "bad", 0: "neutral", 1: "good"}
    color_by_rating = {-1: "#ff7676", 0: "#8a93a6", 1: "#b5e3a2"}
    by_rating = {r["rating"]: r["n"] for r in rows}
    ratings = [-1, 0, 1]
    return {
        "data": [{
            "type": "pie",
            "labels": [labels_by_rating[r] for r in ratings],
            "values": [by_rating.get(r, 0) for r in ratings],
            "marker": {"colors": [color_by_rating[r] for r in ratings]},
            "hole": 0.4,
        }],
        "layout": _layout("Annotation ratings"),
    }


def correction_rate(conn: sqlite3.Connection) -> dict:
    """Weekly % of turns that have a turn_followups row (i.e. were corrected)."""
    rows = conn.execute(
        """
        SELECT strftime('%Y-W%W', t.started_at) AS wk,
               COUNT(*) AS turns,
               COUNT(f.turn_id) AS followups
        FROM turns t
        LEFT JOIN turn_followups f ON f.prev_turn_id = t.id
        WHERE t.started_at >= date('now', '-180 days')
        GROUP BY wk
        ORDER BY wk
        """
    ).fetchall()
    x = [r["wk"] for r in rows]
    y = [(100.0 * r["followups"] / r["turns"]) if r["turns"] else 0.0 for r in rows]
    return {
        "data": [{
            "type": "scatter",
            "mode": "lines+markers",
            "x": x,
            "y": y,
            "name": "% followed up",
            "line": {"color": "#ffb56b"},
        }],
        "layout": _layout("Correction / follow-up rate (weekly)",
                          xaxis={"title": "ISO week"},
                          yaxis={"title": "% of turns", "ticksuffix": "%"}),
    }


def top_clusters(conn: sqlite3.Connection) -> dict:
    """Top 10 prompt_clusters by member_count, horizontal bar."""
    rows = conn.execute(
        """
        SELECT id, COALESCE(label, 'cluster #' || id) AS label, member_count
        FROM prompt_clusters
        ORDER BY member_count DESC
        LIMIT 10
        """
    ).fetchall()
    rows = list(reversed(rows))  # so largest bar appears at top
    return {
        "data": [{
            "type": "bar",
            "orientation": "h",
            "x": [r["member_count"] for r in rows],
            "y": [r["label"] for r in rows],
            "marker": {"color": "#7aa2ff"},
        }],
        "layout": _layout("Top 10 prompt clusters",
                          xaxis={"title": "members"},
                          yaxis={"automargin": True}),
    }


def health(conn: sqlite3.Connection) -> dict:
    """Lightweight system-health snapshot. Returned to the dashboard tile."""
    sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
    turns = conn.execute("SELECT COUNT(*) AS c FROM turns").fetchone()["c"]
    tool_calls = conn.execute("SELECT COUNT(*) AS c FROM tool_calls").fetchone()["c"]
    annotations_n = conn.execute("SELECT COUNT(*) AS c FROM annotations").fetchone()["c"]
    last = conn.execute(
        "SELECT MAX(started_at) AS last_turn FROM turns"
    ).fetchone()["last_turn"]
    vec_count = conn.execute("SELECT COUNT(*) AS c FROM turn_vec").fetchone()["c"]
    coverage = (vec_count / turns) if turns else 0.0
    status = "ok"
    if turns == 0:
        status = "empty"
    elif coverage < 0.5:
        status = "degraded"
    return {
        "status": status,
        "sessions": sessions,
        "turns": turns,
        "tool_calls": tool_calls,
        "annotations": annotations_n,
        "vec_coverage": round(coverage, 4),
        "last_turn_at": last,
    }


CHARTS = {
    "turns_per_day": turns_per_day,
    "tokens_by_model": tokens_by_model,
    "tool_heatmap": tool_heatmap,
    "error_rate": error_rate,
    "latency": latency,
    "annotations": annotations,
    "correction_rate": correction_rate,
    "top_clusters": top_clusters,
}
