"""Full-fidelity replay of a past prompt against the current `claude` CLI.

Looks up a recorded turn, builds a `claude -p ...` command, runs it inside a
fresh workspace dir, and records the result in the `reruns` table. The rerun's
own Stop hook will then ingest the resulting session JSONL with `origin='rerun'`
so the two turns can be diffed in the UI.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone
from difflib import HtmlDiff
from pathlib import Path

from . import DATA_DIR
from .db import connect

RERUN_ROOT = DATA_DIR / "reruns"
RERUN_LOCK_PATH = RERUN_ROOT / ".lock"
DEFAULT_BUDGET_USD = float(os.environ.get("RERUN_BUDGET_USD", "0.50"))
DEFAULT_MODEL_FALLBACK = "opus"
RERUN_TIMEOUT_SECONDS = 300


def resolve_claude_binary() -> str:
    """Resolve the path to the `claude` CLI binary."""
    which = shutil.which("claude")
    if which:
        return which
    fallback = "/opt/homebrew/bin/claude"
    if Path(fallback).exists():
        return fallback
    raise FileNotFoundError("could not locate `claude` binary (tried PATH and /opt/homebrew/bin/claude)")


def _iso_z() -> str:
    """Filesystem-safe UTC ISO timestamp, e.g. 2026-05-13T22-30-15Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _make_workspace(turn_id: int) -> Path:
    ws = RERUN_ROOT / str(turn_id) / _iso_z()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _parse_json_output(stdout: str) -> dict:
    """Best-effort parse of `claude -p --output-format json` stdout.

    The CLI prints a single JSON object. Be tolerant of leading/trailing
    whitespace and the rare case where extra non-JSON noise precedes the
    object.
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Fall back to scanning for the first `{...}` block.
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stdout[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def _extract_response_text(result_json: dict) -> str:
    """Pull the main assistant text out of the CLI's JSON output."""
    if not isinstance(result_json, dict):
        return ""
    for key in ("result", "response", "text", "content"):
        v = result_json.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _insert_rerun(conn: sqlite3.Connection, *, original_turn_id: int, model: str,
                  workspace: Path, status: str, error_message: str | None,
                  result_json: dict, response_text: str, run_at: str,
                  finished_at: str) -> int:
    usage = result_json.get("usage") if isinstance(result_json, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    cur = conn.execute(
        """
        INSERT INTO reruns(
            original_turn_id, replay_turn_id, replay_session_id, model,
            total_cost_usd, num_turns, duration_ms,
            input_tokens, output_tokens,
            response_text, status, error_message,
            workspace_path, run_at, finished_at
        )
        VALUES (?, NULL, ?, ?,  ?, ?, ?,  ?, ?,  ?, ?, ?,  ?, ?, ?)
        """,
        (
            original_turn_id,
            result_json.get("session_id"),
            model,
            result_json.get("total_cost_usd"),
            result_json.get("num_turns"),
            result_json.get("duration_ms"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            response_text or None,
            status,
            error_message,
            str(workspace),
            run_at,
            finished_at,
        ),
    )
    return int(cur.lastrowid)


def run_rerun(turn_id: int, model: str | None = None,
              budget_usd: float = DEFAULT_BUDGET_USD) -> dict:
    """Replay the prompt from `turn_id` against the current `claude` CLI.

    Returns the inserted reruns row as a dict.
    """
    conn = connect()
    turn = conn.execute(
        "SELECT id, user_text, model FROM turns WHERE id = ?", (turn_id,)
    ).fetchone()
    if not turn:
        raise ValueError(f"turn {turn_id} not found")
    user_text = turn["user_text"] or ""
    if not user_text.strip():
        raise ValueError(f"turn {turn_id} has empty user_text")

    chosen_model = model or turn["model"] or DEFAULT_MODEL_FALLBACK
    workspace = _make_workspace(turn_id)
    RERUN_ROOT.mkdir(parents=True, exist_ok=True)
    RERUN_LOCK_PATH.touch(exist_ok=True)

    claude_bin = resolve_claude_binary()
    cmd = [
        claude_bin,
        "-p", user_text,
        "--output-format", "json",
        "--no-session-persistence",
        "--model", chosen_model,
        "--max-budget-usd", f"{budget_usd:g}",
    ]

    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Exclusive lock: only one rerun in flight at a time across this DB.
    with RERUN_LOCK_PATH.open("w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=RERUN_TIMEOUT_SECONDS,
                )
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                rc = proc.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or "" if hasattr(exc, "stdout") else ""
                stderr = (exc.stderr or "") if hasattr(exc, "stderr") else ""
                rc = -1
                stderr = (stderr or "") + f"\n[timeout after {RERUN_TIMEOUT_SECONDS}s]"

            result_json = _parse_json_output(stdout)
            response_text = _extract_response_text(result_json)

            # Detect over-budget from either:
            #   (a) the structured JSON CLI output: subtype starts with "error_max_budget"
            #       or any string in `errors` mentions budget,
            #   (b) the stderr text (older CLIs / edge cases).
            subtype = (result_json.get("subtype") or "").lower() if isinstance(result_json, dict) else ""
            errors_blob = ""
            if isinstance(result_json, dict):
                errs = result_json.get("errors")
                if isinstance(errs, list):
                    errors_blob = " ".join(str(e) for e in errs).lower()
            stderr_low = (stderr or "").lower()
            over_budget = (
                "max_budget" in subtype
                or "budget" in errors_blob
                or ("budget" in stderr_low and ("exceed" in stderr_low or "max" in stderr_low))
            )
            is_error_flag = bool(isinstance(result_json, dict) and result_json.get("is_error"))

            if over_budget:
                status = "over_budget"
                # Prefer the structured `errors` list, else stderr, else the raw JSON.
                if isinstance(result_json, dict) and isinstance(result_json.get("errors"), list) and result_json["errors"]:
                    error_message = "; ".join(str(e) for e in result_json["errors"])[:4000]
                else:
                    error_message = (stderr.strip() or stdout.strip())[:4000] or "budget exceeded"
            elif rc != 0 or is_error_flag or not result_json:
                status = "failure"
                err_blob = stderr.strip() or stdout.strip()
                error_message = err_blob[:4000] or f"exit code {rc}"
            else:
                status = "success"
                error_message = None

            finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # Persist raw CLI artifacts for forensic debugging.
            try:
                (workspace / "stdout.json").write_text(stdout, encoding="utf-8")
                if stderr:
                    (workspace / "stderr.log").write_text(stderr, encoding="utf-8")
                (workspace / "cmd.json").write_text(
                    json.dumps({"cmd": cmd, "rc": rc, "budget_usd": budget_usd}, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

            with conn:
                rerun_id = _insert_rerun(
                    conn,
                    original_turn_id=turn_id,
                    model=chosen_model,
                    workspace=workspace,
                    status=status,
                    error_message=error_message,
                    result_json=result_json,
                    response_text=response_text,
                    run_at=run_at,
                    finished_at=finished_at,
                )

            row = conn.execute("SELECT * FROM reruns WHERE id = ?", (rerun_id,)).fetchone()
            return _row_to_dict(row) or {}
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


# ----------------------------------------------------------------------------
# HTML diff renderer
# ----------------------------------------------------------------------------

_HTML_DIFFER = HtmlDiff(tabsize=4, wrapcolumn=80)


def render_inline_diff(a: str, b: str) -> str:
    """Render a side-by-side HTML diff table comparing two strings.

    The returned HTML is safe to embed inside a `<div>` (it's a `<table>`).
    Empty inputs are coerced to empty strings.
    """
    a_lines = (a or "").splitlines() or [""]
    b_lines = (b or "").splitlines() or [""]
    return _HTML_DIFFER.make_table(
        a_lines, b_lines,
        fromdesc="original",
        todesc="rerun",
        context=False,
    )


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="prompt_telemetry.rerun",
        description="Replay a recorded turn against the current `claude` CLI.",
    )
    p.add_argument("turn_id", type=int, help="ID of the turn to replay")
    p.add_argument("--model", default=None,
                   help="Override model (default: original turn's model, falling back to 'opus')")
    p.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD,
                   help=f"Max cost cap passed to `claude --max-budget-usd` (default: {DEFAULT_BUDGET_USD})")
    args = p.parse_args(argv)

    try:
        row = run_rerun(args.turn_id, model=args.model, budget_usd=args.budget_usd)
    except Exception as exc:
        print(json.dumps({"error": str(exc), "turn_id": args.turn_id}), file=sys.stderr)
        return 1

    summary = {
        "rerun_id": row.get("id"),
        "original_turn_id": row.get("original_turn_id"),
        "status": row.get("status"),
        "model": row.get("model"),
        "total_cost_usd": row.get("total_cost_usd"),
        "duration_ms": row.get("duration_ms"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "replay_session_id": row.get("replay_session_id"),
        "workspace_path": row.get("workspace_path"),
        "error_message": row.get("error_message"),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
