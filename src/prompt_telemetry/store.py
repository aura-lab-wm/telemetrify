import sqlite3
from typing import Literal

from .db import serialize_embedding
from .raw_archive import compress
from .transcript import Turn


def upsert_session(conn: sqlite3.Connection, turn: Turn) -> None:
    conn.execute(
        """
        INSERT INTO sessions(id, started_at, last_turn_at, cwd, git_branch,
                             project_dir, transcript_path, entrypoint, user_type, cli_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_turn_at    = excluded.last_turn_at,
            transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
            cli_version     = COALESCE(excluded.cli_version, sessions.cli_version)
        """,
        (
            turn.session_id, turn.started_at, turn.finished_at or turn.started_at,
            turn.cwd or "", turn.git_branch,
            turn.project_dir, turn.transcript_path, turn.entrypoint, turn.user_type,
            turn.cli_version,
        ),
    )


def insert_turn(
    conn: sqlite3.Connection,
    turn: Turn,
    embedding: list[float] | None,
    *,
    origin: str = "organic",
    prompt_embedding: list[float] | None = None,
) -> int | None:
    """Return inserted turn_id, or None if the user_uuid was already recorded."""
    raw_z = compress(turn.raw_json) if turn.raw_json else None
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO turns(
            session_id, prompt_id, user_uuid, parent_uuid,
            user_text, assistant_text, thinking_text, model,
            started_at, finished_at, latency_ms,
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            cwd, git_branch, cli_version,
            attribution_skill, attribution_plugin,
            tool_call_count, assistant_message_count, raw_json_z, origin
        )
        VALUES (?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?,  ?, ?,  ?, ?, ?, ?)
        """,
        (
            turn.session_id, turn.prompt_id, turn.user_uuid, turn.parent_uuid,
            turn.user_text, turn.assistant_text, turn.thinking_text, turn.model,
            turn.started_at, turn.finished_at, turn.latency_ms,
            turn.input_tokens, turn.output_tokens, turn.cache_creation_tokens, turn.cache_read_tokens,
            turn.cwd, turn.git_branch, turn.cli_version,
            turn.attribution_skill, turn.attribution_plugin,
            turn.tool_call_count, turn.assistant_message_count, raw_z, origin,
        ),
    )
    if cur.rowcount == 0:
        return None
    turn_id = cur.lastrowid

    for tc in turn.tool_calls:
        conn.execute(
            """
            INSERT INTO tool_calls(turn_id, seq, tool_name, tool_use_id,
                                   input_json, output_text, is_error, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (turn_id, tc.seq, tc.tool_name, tc.tool_use_id,
             tc.input_json, tc.output_text, 1 if tc.is_error else 0, tc.started_at),
        )

    if embedding is not None:
        conn.execute(
            "INSERT INTO turn_vec(turn_id, embedding) VALUES (?, ?)",
            (turn_id, serialize_embedding(embedding)),
        )

    if prompt_embedding is not None:
        conn.execute(
            "INSERT INTO prompt_vec(turn_id, embedding) VALUES (?, ?)",
            (turn_id, serialize_embedding(prompt_embedding)),
        )

    return turn_id


def record_ingest_run(
    conn: sqlite3.Connection,
    source: Literal["hook", "backfill"],
    started_at: str,
    finished_at: str,
    inserted: int,
    skipped: int,
    errors: int,
    note: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_runs(started_at, finished_at, source, inserted, skipped, errors, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (started_at, finished_at, source, inserted, skipped, errors, note),
    )
