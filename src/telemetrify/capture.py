import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import LOG_PATH, DATA_DIR
from .db import connect
from .store import upsert_session, insert_turn, record_ingest_run
from .transcript import parse_latest_turn


def _log(msg: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _resolve_transcript(payload: dict) -> Path | None:
    explicit = payload.get("transcript_path")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        return None
    root = Path.home() / ".claude" / "projects"
    matches = list(root.glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def _detect_origin(payload: dict) -> str:
    """Tag turns originating from a rerun workspace differently so analysis can split them."""
    cwd = payload.get("cwd") or ""
    if "/data/reruns/" in cwd:
        return "rerun"
    return "organic"


def main() -> int:
    # Re-entrancy guard: telemetrify itself can spawn `claude -p` (the claude_cli
    # LLM tier). That child session's Stop hook would otherwise capture our own
    # internal calls into the corpus and trigger a nested grade. The backend
    # sets this flag in the child env so we bail before touching the DB.
    if os.environ.get("TELEMETRIFY_NO_CAPTURE"):
        return 0
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = skipped = errors = 0
    note = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        _log(f"bad stdin: {exc}")
        return 0

    if payload.get("agent_id"):
        return 0

    try:
        transcript_path = _resolve_transcript(payload)
        if transcript_path is None:
            _log(f"no transcript for payload: {payload}")
            return 0

        turn = parse_latest_turn(transcript_path)
        if turn is None:
            return 0

        conn = connect()
        with conn:
            upsert_session(conn, turn)
            from .embed import embed_turn, embed_prompt
            full_vec = embed_turn(turn.user_text, turn.assistant_text)
            prompt_vec = embed_prompt(turn.user_text)
            origin = _detect_origin(payload)
            turn_id = insert_turn(conn, turn, full_vec,
                                   origin=origin, prompt_embedding=prompt_vec)
            if turn_id is None:
                skipped = 1
                note = "already recorded"
            else:
                inserted = 1
                note = f"turn_id={turn_id}"
                # Best-effort post-processing: follow-up detection + nearest-cluster assignment.
                try:
                    from .followups import detect_for_turn
                    detect_for_turn(conn, turn_id)
                except Exception:
                    _log(f"followup detect failed: {traceback.format_exc()}")
                try:
                    from .cluster import assign_nearest_cluster
                    assign_nearest_cluster(conn, turn_id, prompt_vec)
                except Exception:
                    _log(f"cluster assign failed: {traceback.format_exc()}")
                # Round A1: inline LLM-as-Judge grade. Fail-silent (capture must
                # never block the user). 10s timeout via the SDK's own timeout.
                try:
                    from .ai.grader import grade_turn
                    grade_turn(conn, turn_id, timeout=10.0)
                except Exception:
                    _log(f"auto-grade failed: {traceback.format_exc()}")
            finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            record_ingest_run(conn, "hook", started_at, finished_at,
                              inserted, skipped, errors, note)
        return 0
    except Exception:
        _log(f"capture failed:\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
