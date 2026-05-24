"""Streaming bulk export of turns.

Both `export_jsonl` and `export_csv` yield text chunks so the FastAPI handler
can pipe them into a StreamingResponse without buffering the full DB into RAM.

Filters are reused from `telemetrify.search.Filters` so the export honors
the same WHERE fragment that powers the UI.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Iterator

from .search import Filters


# Flat scalar columns suitable for CSV. We deliberately exclude
# raw_json_z (BLOB), thinking_text (large), and tool_calls (nested).
_CSV_COLUMNS = [
    "id", "session_id", "prompt_id", "model", "started_at", "finished_at",
    "latency_ms", "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens",
    "cwd", "git_branch", "cli_version",
    "attribution_skill", "attribution_plugin",
    "tool_call_count", "assistant_message_count", "origin",
    "user_text", "assistant_text",
    "annotation_rating", "annotation_label",
]


def _turn_query(filters: Filters) -> tuple[str, list]:
    where = ("WHERE " + filters.where) if filters.where else ""
    sql = f"""
        SELECT t.*, a.rating AS annotation_rating, a.label AS annotation_label,
               a.tags AS annotation_tags, a.notes AS annotation_notes,
               a.expected_behavior AS annotation_expected_behavior
        FROM turns t
        LEFT JOIN annotations a ON a.turn_id = t.id
        {where}
        ORDER BY t.started_at ASC, t.id ASC
    """
    return sql, list(filters.params)


def export_jsonl(conn: sqlite3.Connection, filters: Filters) -> Iterator[str]:
    """Yield one JSON line per turn. Each line embeds the turn's tool_calls
    and (if present) annotation fields. The trailing newline is included."""
    sql, params = _turn_query(filters)
    cur = conn.execute(sql, params)
    for row in cur:
        turn = dict(row)
        turn.pop("raw_json_z", None)  # never serialize compressed blob
        annotation = None
        if turn.get("annotation_rating") is not None or turn.get("annotation_label"):
            annotation = {
                "rating": turn.pop("annotation_rating", None),
                "label": turn.pop("annotation_label", None),
                "tags": turn.pop("annotation_tags", None),
                "notes": turn.pop("annotation_notes", None),
                "expected_behavior": turn.pop("annotation_expected_behavior", None),
            }
        else:
            for k in ("annotation_rating", "annotation_label", "annotation_tags",
                      "annotation_notes", "annotation_expected_behavior"):
                turn.pop(k, None)

        tool_calls = [
            dict(tc) for tc in conn.execute(
                "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY seq ASC",
                (turn["id"],),
            ).fetchall()
        ]
        record = {"turn": turn, "tool_calls": tool_calls, "annotation": annotation}
        yield json.dumps(record, default=str, ensure_ascii=False) + "\n"


def export_csv(conn: sqlite3.Connection, filters: Filters) -> Iterator[str]:
    """Yield CSV rows (header first). Flat scalar columns only — no tool_calls,
    thinking_text, or raw_json. Each yielded string is one or more complete
    lines ending in CRLF (csv module default)."""
    sql, params = _turn_query(filters)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    yield buf.getvalue()
    buf.seek(0); buf.truncate(0)

    cur = conn.execute(sql, params)
    for row in cur:
        d = dict(row)
        writer.writerow([d.get(col) for col in _CSV_COLUMNS])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
