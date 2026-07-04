"""Streaming bulk export of turns.

Both `export_jsonl` and `export_csv` yield text chunks so the FastAPI handler
can pipe them into a StreamingResponse without buffering the full DB into RAM.

Filters are reused from `telemetrify.search.Filters` so the export honors
the same WHERE fragment that powers the UI.

Threading note (important, do not "simplify" away): the FastAPI route wraps
these generators in `StreamingResponse`. Starlette drives a sync generator via
`iterate_in_threadpool`, which dispatches EVERY `next()` call independently
through anyio's worker thread pool -- it does NOT pin the generator to one
thread for its whole lifetime. `telemetrify.db.connect()`'s cached connection
is thread-affine (`check_same_thread=True`, one connection per OS thread), so
using it here crashes as soon as two consecutive `next()` calls land on
different worker threads -- which is near-certain on any real, non-trivial
result set (reliably reproduced on a full unfiltered export). Each export
function therefore opens its OWN dedicated connection via
`db.connect_uncached()` (uncached, `check_same_thread=False`) for the
lifetime of the export, and closes it in `finally` -- see the docstring on
`connect_uncached` for why this is safe despite the cross-thread access.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Iterator

from . import db
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

# The `turns` columns each CSV row actually reads (subset of _CSV_COLUMNS,
# minus the two annotation_* columns which come from the joined table).
_CSV_TURN_COLUMNS = [c for c in _CSV_COLUMNS if not c.startswith("annotation_")]

# All `turns` columns except raw_json_z (the compressed full-conversation
# BLOB): jsonl embeds the full turn for fidelity, but the blob is dropped
# immediately after fetch, so there is no reason to pull it off disk at all.
_JSONL_TURN_COLUMNS = [
    "id", "session_id", "prompt_id", "user_uuid", "parent_uuid",
    "user_text", "assistant_text", "thinking_text", "model",
    "started_at", "finished_at", "latency_ms", "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens",
    "cwd", "git_branch", "cli_version",
    "attribution_skill", "attribution_plugin",
    "tool_call_count", "assistant_message_count", "origin",
]


def _turn_query(
    filters: Filters, columns: list[str], *, full_annotation: bool
) -> tuple[str, list]:
    where = ("WHERE " + filters.where) if filters.where else ""
    select = ", ".join(f"t.{c}" for c in columns)
    select += ", a.rating AS annotation_rating, a.label AS annotation_label"
    if full_annotation:
        select += (
            ", a.tags AS annotation_tags, a.notes AS annotation_notes, "
            "a.expected_behavior AS annotation_expected_behavior"
        )
    sql = f"""
        SELECT {select}
        FROM turns t
        LEFT JOIN annotations a ON a.turn_id = t.id
        {where}
        ORDER BY t.started_at ASC, t.id ASC
    """
    return sql, list(filters.params)


def export_jsonl(conn: sqlite3.Connection, filters: Filters) -> Iterator[str]:
    """Yield one JSON line per turn. Each line embeds the turn's tool_calls
    and (if present) annotation fields. The trailing newline is included.

    `conn` is accepted only for call-site compatibility with the existing
    FastAPI route signature; it is deliberately NOT the connection used to
    run the query -- see the module docstring for why the shared, thread-
    pinned connection from `db.connect()` is unsafe here. A dedicated
    connection is opened and closed around the whole export instead.
    """
    del conn  # intentionally unused -- see docstring
    export_conn = db.connect_uncached()
    try:
        sql, params = _turn_query(filters, _JSONL_TURN_COLUMNS, full_annotation=True)
        cur = export_conn.execute(sql, params)
        for row in cur:
            turn = dict(row)
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
                dict(tc) for tc in export_conn.execute(
                    "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY seq ASC",
                    (turn["id"],),
                ).fetchall()
            ]
            record = {"turn": turn, "tool_calls": tool_calls, "annotation": annotation}
            yield json.dumps(record, default=str, ensure_ascii=False) + "\n"
    finally:
        export_conn.close()


def export_csv(conn: sqlite3.Connection, filters: Filters) -> Iterator[str]:
    """Yield CSV rows (header first). Flat scalar columns only — no tool_calls,
    thinking_text, or raw_json. Each yielded string is one or more complete
    lines ending in CRLF (csv module default).

    `conn` is accepted only for call-site compatibility with the existing
    FastAPI route signature; see `export_jsonl`'s docstring and the module
    docstring for why a dedicated connection is opened internally instead.
    """
    del conn  # intentionally unused -- see docstring
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    yield buf.getvalue()
    buf.seek(0); buf.truncate(0)

    export_conn = db.connect_uncached()
    try:
        sql, params = _turn_query(filters, _CSV_TURN_COLUMNS, full_annotation=False)
        cur = export_conn.execute(sql, params)
        for row in cur:
            d = dict(row)
            writer.writerow([d.get(col) for col in _CSV_COLUMNS])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
    finally:
        export_conn.close()
