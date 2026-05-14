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

# ─── Telemetry · Vercel Mode Plotly theme ────────────────────────────────
# Pure-black canvas. Single signature orange (#ff5c00). Geist Mono throughout.
# Aligns with the v-page CSS scope shared with /ask and /queue.
LEDGER_PALETTE = ["#ff5c00", "#ff7d33", "#f59e0b", "#22c55e", "#ef4444",
                  "#a78bfa", "#06b6d4", "#facc15", "#fb7185", "#94a3b8"]

_LEDGER_AXIS = {
    "gridcolor":     "rgba(255,255,255,0.05)",
    "linecolor":     "rgba(255,255,255,0.10)",
    "zerolinecolor": "rgba(255,255,255,0.07)",
    "tickcolor":     "rgba(255,255,255,0.10)",
    "tickfont": {"family": "Geist Mono, JetBrains Mono, monospace",
                 "color": "#a1a1aa", "size": 10},
    "title": {"font": {"family": "Geist, Inter, sans-serif",
                       "color": "#71717a", "size": 11}},
    "automargin": True,
}

_LEDGER_BASE = {
    "paper_bgcolor": "#0a0a0a",
    "plot_bgcolor":  "#0a0a0a",
    "font": {"family": "Geist Mono, JetBrains Mono, monospace",
             "color": "#a1a1aa", "size": 11},
    "margin": {"l": 56, "r": 24, "t": 56, "b": 44},
    "xaxis": _LEDGER_AXIS,
    "yaxis": _LEDGER_AXIS,
    "colorway": LEDGER_PALETTE,
    "legend": {"font": {"family": "Geist, Inter, sans-serif", "size": 11,
                        "color": "#a1a1aa"},
               "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
               "orientation": "h", "y": -0.18, "x": 0},
    "hoverlabel": {"bgcolor": "#111111", "bordercolor": "rgba(255,92,0,0.4)",
                   "font": {"family": "Geist Mono, monospace",
                            "color": "#fafafa", "size": 11}},
}


def _layout(title: str, exhibit: str | None = None, **extra: Any) -> dict:
    """Build a Ledger-themed Plotly layout.

    `exhibit` is the figure caption ("Fig. 3 · weekly latency p50/p95") — it
    becomes the chart title, rendered serif in lamplit cream.
    """
    caption = f"{exhibit} · {title}" if exhibit else title
    layout = {
        **_LEDGER_BASE,
        "title": {
            "text": caption,
            "font": {"family": "Geist, Inter, sans-serif", "size": 15,
                     "color": "#fafafa", "weight": 600},
            "x": 0, "xanchor": "left", "y": 0.97, "yanchor": "top",
            "pad": {"l": 0, "t": 4},
        },
    }
    for axis_key in ("xaxis", "yaxis"):
        if axis_key in extra:
            merged = {**_LEDGER_AXIS, **extra.pop(axis_key)}
            layout[axis_key] = merged
    layout.update(extra)
    return layout


def turns_per_day(conn: sqlite3.Connection) -> dict:
    """Line chart of turn counts per UTC day, last 90 days."""
    rows = conn.execute(
        """
        SELECT date(started_at) AS d, COUNT(*) AS n
        FROM turns
        WHERE started_at >= date('now', '-90 days')
        GROUP BY d ORDER BY d
        """
    ).fetchall()
    x = [r["d"] for r in rows]
    y = [r["n"] for r in rows]
    return {
        "data": [{
            "type": "scatter", "mode": "lines",
            "x": x, "y": y, "name": "turns",
            "line": {"color": "#ff5c00", "width": 1.5, "shape": "spline", "smoothing": 0.4},
            "fill": "tozeroy",
            "fillcolor": "rgba(255,92,0,0.10)",
        }],
        "layout": _layout("turns per day", exhibit="Fig. 1",
                          xaxis={"title": "day"},
                          yaxis={"title": "turns / day"}),
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

    traces = []
    for i, (model, by_day) in enumerate(sorted(per_model.items())):
        traces.append({
            "type": "scatter", "mode": "lines",
            "stackgroup": "one",
            "x": days,
            "y": [by_day.get(d, 0) for d in days],
            "name": model,
            "line": {"color": LEDGER_PALETTE[i % len(LEDGER_PALETTE)], "width": 0.5},
        })
    return {
        "data": traces,
        "layout": _layout("tokens by model (stacked, daily)", exhibit="Fig. 2",
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
    # Custom warm colorscale: lamplit ink → phosphor amber → bright cream
    ledger_scale = [
        [0.00, "#0a0a0a"],
        [0.15, "rgba(255,255,255,0.10)"],
        [0.40, "#52525b"],
        [0.70, "#f59e0b"],
        [1.00, "#ff5c00"],
    ]
    return {
        "data": [{
            "type": "heatmap",
            "x": weeks, "y": tools, "z": z,
            "colorscale": ledger_scale,
            "showscale": True,
            "colorbar": {"thickness": 8, "len": 0.7,
                         "tickfont": {"family": "Geist Mono", "size": 9, "color": "#a1a1aa"},
                         "outlinecolor": "rgba(255,255,255,0.10)"},
            "xgap": 1, "ygap": 1,
        }],
        "layout": _layout("tool calls heatmap (tool × ISO week)", exhibit="Fig. 3",
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
            "type": "scatter", "mode": "lines+markers",
            "x": x, "y": y, "name": "% with tool error",
            "line": {"color": "#ef4444", "width": 1.5},
            "marker": {"color": "#ef4444", "size": 5},
        }],
        "layout": _layout("tool error rate (weekly)", exhibit="Fig. 4",
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
             "name": "p50", "line": {"color": "#22c55e", "width": 1.5},
             "marker": {"size": 5}},
            {"type": "scatter", "mode": "lines+markers", "x": weeks, "y": p95,
             "name": "p95", "line": {"color": "#ff5c00", "width": 1.5},
             "marker": {"size": 5}},
        ],
        "layout": _layout("latency (weekly p50 / p95)", exhibit="Fig. 5",
                          xaxis={"title": "ISO week"},
                          yaxis={"title": "ms"}),
    }


def annotations(conn: sqlite3.Connection) -> dict:
    """Pie chart of annotation rating buckets (-1 / 0 / +1)."""
    rows = conn.execute(
        "SELECT rating, COUNT(*) AS n FROM annotations GROUP BY rating"
    ).fetchall()
    labels_by_rating = {-1: "bad", 0: "neutral", 1: "good"}
    color_by_rating = {-1: "#ef4444", 0: "#8a8270", 1: "#22c55e"}
    by_rating = {r["rating"]: r["n"] for r in rows}
    ratings = [-1, 0, 1]
    return {
        "data": [{
            "type": "pie",
            "labels": [labels_by_rating[r] for r in ratings],
            "values": [by_rating.get(r, 0) for r in ratings],
            "marker": {"colors": [color_by_rating[r] for r in ratings],
                       "line": {"color": "#0a0a0a", "width": 2}},
            "hole": 0.55,
            "textfont": {"family": "Newsreader, serif", "color": "#fafafa", "size": 12},
        }],
        "layout": _layout("annotation ratings", exhibit="Fig. 6"),
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
            "type": "scatter", "mode": "lines+markers",
            "x": x, "y": y, "name": "% followed up",
            "line": {"color": "#ff5c00", "width": 1.5},
            "marker": {"size": 5},
            "fill": "tozeroy", "fillcolor": "rgba(255,92,0,0.08)",
        }],
        "layout": _layout("correction / follow-up rate (weekly)", exhibit="Fig. 7",
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
            "type": "bar", "orientation": "h",
            "x": [r["member_count"] for r in rows],
            "y": [(r["label"][:48] + "…") if len(r["label"] or "") > 48 else (r["label"] or f"cluster #{r['id']}") for r in rows],
            "marker": {"color": "#22c55e", "line": {"color": "#0a0a0a", "width": 1}},
            "text": [str(r["member_count"]) for r in rows],
            "textposition": "outside",
            "textfont": {"family": "Geist Mono, monospace", "size": 10, "color": "#a1a1aa"},
            "cliponaxis": False,
        }],
        "layout": _layout("top 10 prompt clusters", exhibit="Fig. 8",
                          xaxis={"title": "members", "showgrid": False},
                          yaxis={"automargin": True,
                                 "tickfont": {"family": "Newsreader, serif",
                                              "size": 11, "color": "#a1a1aa"}},
                          bargap=0.35),
    }


def cache_efficiency(conn: sqlite3.Connection) -> dict:
    """Weekly prompt-caching hit ratio: cache_read / (cache_read + cache_creation).
    A direct cost signal — higher = more savings from the Anthropic cache."""
    rows = conn.execute(
        """
        SELECT strftime('%Y-W%W', started_at) AS wk,
               SUM(COALESCE(cache_read_tokens, 0))     AS hit,
               SUM(COALESCE(cache_creation_tokens, 0)) AS miss,
               COUNT(*) AS turns
        FROM turns
        WHERE started_at >= date('now', '-180 days')
        GROUP BY wk ORDER BY wk
        """
    ).fetchall()
    weeks = [r["wk"] for r in rows]
    hits = [int(r["hit"] or 0) for r in rows]
    misses = [int(r["miss"] or 0) for r in rows]
    pct = [
        (100.0 * h / (h + m)) if (h + m) else 0.0
        for h, m in zip(hits, misses)
    ]
    return {
        "data": [
            {"type": "bar", "x": weeks, "y": hits, "name": "cache read (hit)",
             "marker": {"color": "#22c55e"}, "yaxis": "y2"},
            {"type": "bar", "x": weeks, "y": misses, "name": "cache creation (miss)",
             "marker": {"color": "#52525b"}, "yaxis": "y2"},
            {"type": "scatter", "mode": "lines+markers", "x": weeks, "y": pct,
             "name": "hit ratio", "line": {"color": "#ff5c00", "width": 2},
             "marker": {"size": 6}, "yaxis": "y"},
        ],
        "layout": _layout("cache efficiency (weekly hit ratio + token volume)",
                          exhibit="Fig. 9",
                          barmode="stack",
                          xaxis={"title": "ISO week"},
                          yaxis={"title": "hit %", "ticksuffix": "%",
                                 "range": [0, 100], "side": "left"},
                          yaxis2={**_LEDGER_AXIS, "title": {"text": "tokens",
                                     "font": {"family": "Newsreader, serif",
                                              "color": "#71717a", "size": 11}},
                                  "overlaying": "y", "side": "right",
                                  "showgrid": False}),
    }


def cluster_correction_breakdown(conn: sqlite3.Connection) -> dict:
    """For the top-10 clusters by size, what fraction of members triggered a
    follow-up / correction? This is the telemetry-testing centerpiece — it
    reveals which prompt families you keep correcting Claude on."""
    rows = conn.execute(
        """
        SELECT pc.id, COALESCE(pc.label, 'cluster #' || pc.id) AS label,
               pc.member_count,
               COUNT(DISTINCT f.turn_id) AS corrected
        FROM prompt_clusters pc
        JOIN turn_cluster tc ON tc.cluster_id = pc.id
        LEFT JOIN turn_followups f ON f.turn_id = tc.turn_id
        GROUP BY pc.id
        ORDER BY pc.member_count DESC
        LIMIT 10
        """
    ).fetchall()
    rows = list(reversed(rows))  # largest at top
    labels = [(r["label"][:46] + "…") if len(r["label"]) > 46 else r["label"] for r in rows]
    total = [int(r["member_count"]) for r in rows]
    corrected = [int(r["corrected"]) for r in rows]
    clean = [t - c for t, c in zip(total, corrected)]
    pct = [(100.0 * c / t) if t else 0.0 for c, t in zip(corrected, total)]
    return {
        "data": [
            {"type": "bar", "orientation": "h", "x": clean, "y": labels,
             "name": "clean", "marker": {"color": "#22c55e"}},
            {"type": "bar", "orientation": "h", "x": corrected, "y": labels,
             "name": "corrected", "marker": {"color": "#ef4444"},
             "text": [f"{p:.0f}%" if p > 0 else "" for p in pct],
             "textposition": "outside",
             "textfont": {"family": "Geist Mono, monospace", "size": 10,
                          "color": "#ef4444"},
             "cliponaxis": False},
        ],
        "layout": _layout("cluster correction breakdown (top 10 by size)",
                          exhibit="Fig. 10",
                          barmode="stack",
                          xaxis={"title": "members"},
                          yaxis={"automargin": True,
                                 "tickfont": {"family": "Newsreader, serif",
                                              "size": 11, "color": "#a1a1aa"}},
                          bargap=0.35),
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
    "cache_efficiency": cache_efficiency,
    "cluster_correction_breakdown": cluster_correction_breakdown,
}
