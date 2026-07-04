"""run_events ledger — the performance backbone.

One row per captured tool_call that "does work" (Bash first; extensible),
denormalizing session_id + tool_name so analysis never needs the hand-join
through turns/tool_calls (tool_calls has NO session_id column — that
hand-join silently no-ops and has caused real analysis bugs).

Three entry points:
  - `derive_for_turn(conn, turn_id, session_id, tool_calls, project, source)`
    called inline from store.insert_turn at capture time, and from the
    backfill path. Idempotent per turn: deletes prior run_events for the
    turn before re-inserting, so re-running a backfill can't double-count.
  - `stamp_outcomes_for_turn(conn, turn_id, project, source)` — the
    outcome-stamping layer (#3): matches the per-project regexes against
    each run_event's tool RESULT output and writes outcome_tag.
  - `backfill_all(conn, log)` — walk every existing turn's Bash tool_calls
    and populate run_events. Idempotent via the per-turn delete.

Discipline (HARD): exit_code / duration_ms are NULL where not actually
derivable from the captured record. We never fabricate them. outcome_tag
is matched against OUTPUT ONLY — never input_json or a file's contents
(see telemetrify.outcome_rules).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from . import outcome_rules


# Tools whose calls become run_events rows. Bash is the primary one; the
# list is extensible. Read-only tools deliberately excluded — they don't
# "run" anything whose outcome we'd track.
RUN_TOOL_NAMES = {"Bash"}


def _project_from_cwd(cwd: str | None) -> str:
    return (cwd or "").strip()


def _extract_command(tool_name: str, input_json: str | None) -> str | None:
    """Pull the human-meaningful command string out of a tool_call input."""
    if not input_json:
        return None
    try:
        obj = json.loads(input_json)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    # Bash: {"command": "...", "description": "..."}. Prefer the command.
    cmd = obj.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd
    # Fallback for non-Bash actor tools that might be added later.
    desc = obj.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc
    return None


def _in_placeholders(items) -> str:
    """Return a comma-joined "?,?,..." placeholder string sized to len(items)
    for use inside a SQL `IN (...)` clause, or the two-char empty-string SQL
    literal `''` (matches no real value) when items is empty.

    Built via plain string concatenation and deliberately NEVER passed through
    old-style `%`-interpolation: a query string assembled this way can already
    contain literal percent signs (e.g. strftime's `%Y`/`%W`, or a LIKE
    pattern's `%`), and running old-style percent-formatting on top of that
    collides with those literal escapes and raises
    `ValueError: unsupported format character '...'`. Always inline the
    result directly into an f-string instead.
    """
    return ",".join("?" * len(items)) if items else "''"


def _load_tool_results(conn: sqlite3.Connection, turn_id: int) -> dict[int, dict]:
    """Map tool_call.id → {tool_name, tool_use_id, input_json, output_text,
    started_at, is_error} for the turn's actor tool_calls."""
    placeholders = _in_placeholders(RUN_TOOL_NAMES)
    rows = conn.execute(
        f"""
        SELECT id, tool_name, tool_use_id, input_json, output_text,
               started_at, is_error
        FROM tool_calls
        WHERE turn_id = ? AND tool_name IN ({placeholders})
        ORDER BY seq ASC
        """,
        (turn_id, *RUN_TOOL_NAMES),
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def derive_for_turn(
    conn: sqlite3.Connection,
    turn_id: int,
    session_id: str,
    *,
    project: str | None,
    source: str,
    tool_calls: Iterable | None,
    commit: bool = True,
) -> int:
    """Insert run_events rows for the turn's actor tool_calls and stamp
    their outcomes from the captured output. Returns the count inserted.

    `tool_calls` is the list of telemetrify.transcript.ToolCall objects
    captured for the turn (we only read tool_name + input_json + started_at
    + tool_use_id). When called from the backfill path where we don't have
    ToolCall objects, pass None and we read the actor rows from tool_calls
    directly.

    Idempotent per turn: any prior run_events for this turn are deleted
    first, so a re-backfill can't double-count.

    `commit`: when True (default, standalone/backfill callers) the DELETE +
    INSERTs are wrapped in a transaction and committed here. When False
    (the capture path, which is already inside `store.insert_turn` → the
    caller's `with conn:` block) we run the statements bare so they join the
    caller's transaction — a nested `with conn:` here would COMMIT early and
    "flush a partial turn", defeating the rollback the caller's except-
    block relies on (the same anti-pattern the grader was moved out of the
    capture transaction for; see capture.py:107).
    """
    # Resolve the turn's session_id + cwd when not handed to us (backfill
    # path passes project=None; we look it up).
    if not session_id:
        row = conn.execute(
            "SELECT session_id, cwd FROM turns WHERE id = ?", (turn_id,)
        ).fetchone()
        if not row:
            return 0
        session_id = row["session_id"] or ""
        if project is None:
            project = _project_from_cwd(row["cwd"])

    # Gather actor tool_calls. At capture time we have the in-memory
    # ToolCall list; at backfill time we read from tool_calls.
    actor_rows: list[dict] = []
    if tool_calls is not None:
        for tc in tool_calls:
            if tc.tool_name in RUN_TOOL_NAMES:
                actor_rows.append({
                    "tool_name": tc.tool_name,
                    "tool_use_id": tc.tool_use_id,
                    "input_json": tc.input_json,
                    "output_text": tc.output_text,
                    "started_at": tc.started_at,
                    "is_error": tc.is_error,
                })
    if not actor_rows:
        # Backfill path: read from the DB.
        actor_rows = list(_load_tool_results(conn, turn_id).values())

    def _body() -> None:
        # Idempotent: clear prior rows for this turn before re-inserting.
        conn.execute("DELETE FROM run_events WHERE turn_id = ?", (turn_id,))
        for r in actor_rows:
            command = _extract_command(r["tool_name"], r.get("input_json"))
            output_text = r.get("output_text") or ""
            # Outcome matched against OUTPUT ONLY.
            outcome_tag = outcome_rules.match_output(project, r["tool_name"], output_text)
            row_source = "auto-output-match" if outcome_tag else source
            conn.execute(
                """
                INSERT INTO run_events(
                    turn_id, session_id, tool_name, command,
                    exit_code, duration_ms, outcome_tag, source, started_at
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    turn_id, session_id, r["tool_name"], command,
                    outcome_tag, row_source, r.get("started_at"),
                ),
            )

    if commit:
        with conn:
            _body()
    else:
        _body()
    return len(actor_rows)


def stamp_outcomes_for_turn(
    conn: sqlite3.Connection,
    turn_id: int,
    *,
    project: str | None = None,
    source: str = "auto-output-match",
) -> int:
    """Re-stamp outcome_tag for every run_event of this turn from its tool
    result output. Returns the count of rows now carrying an outcome_tag.

    Used by the outcome-stamping layer (#3) at turn-finish and to refresh
    outcomes after a config edit. Implemented as a re-derive (delete +
    re-insert from the captured tool_calls) so the per-turn linkage to
    tool_calls stays exact (matched by started_at, not a fragile OFFSET).
    Idempotent.
    """
    row = conn.execute(
        "SELECT session_id, cwd FROM turns WHERE id = ?", (turn_id,)
    ).fetchone()
    if not row:
        return 0
    if project is None:
        project = _project_from_cwd(row["cwd"])
    derive_for_turn(
        conn, turn_id, row["session_id"], project=project, source=source,
        tool_calls=None,
    )
    return conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE turn_id = ? AND outcome_tag IS NOT NULL",
        (turn_id,),
    ).fetchone()[0]


def manual_tag(
    conn: sqlite3.Connection,
    turn_id: int,
    outcome: str,
    *,
    entity: str | None = None,
) -> int:
    """Manually stamp outcome_tag on a turn's run_events. `entity` may be a
    run_event id or a tool_use_id; if None, stamps all the turn's run_events.
    Returns the count stamped."""
    with conn:
        if entity is not None:
            # Try as run_event id first (integer), then tool_use_id.
            if entity.isdigit():
                cur = conn.execute(
                    "UPDATE run_events SET outcome_tag = ?, source = 'manual' "
                    "WHERE id = ? AND turn_id = ?",
                    (outcome, int(entity), turn_id),
                )
                if cur.rowcount:
                    return cur.rowcount
            # Resolve tool_use_id -> the ONE run_event it produced.
            #
            # run_events has no tool_use_id column (schema gap). The prior
            # query worked around that with
            #   WHERE turn_id = ? AND tool_name IN (
            #       SELECT tool_name FROM tool_calls WHERE tool_use_id = ?)
            # — but that subquery only recovers the TOOL NAME ("Bash"), not
            # which specific call, so the outer UPDATE matched (and
            # silently re-tagged) every Bash run_event in the turn whenever
            # the turn had 2+ Bash calls. There was no predicate on the
            # requested tool_use_id at all past that point.
            #
            # run_events rows are always produced 1:1, in the same relative
            # order, from a turn's actor tool_calls (RUN_TOOL_NAMES) — both
            # the capture-time path (iterates the passed-in tool_calls list,
            # already in seq order) and the backfill path
            # (_load_tool_results, `ORDER BY seq ASC`) preserve that
            # ordering, and each INSERT gets the next autoincrement id. So
            # the Nth actor tool_call (by seq) among the turn's tool_calls
            # corresponds exactly to the Nth run_event (by id) for the turn
            # — a reliable positional join given the schema as it stands,
            # and one that (unlike matching on started_at) still
            # disambiguates two Bash calls issued within the same assistant
            # message, which share an identical started_at.
            placeholders = _in_placeholders(RUN_TOOL_NAMES)
            actor_tool_use_ids = [
                r["tool_use_id"] for r in conn.execute(
                    f"""
                    SELECT tool_use_id FROM tool_calls
                    WHERE turn_id = ? AND tool_name IN ({placeholders})
                    ORDER BY seq ASC
                    """,
                    (turn_id, *RUN_TOOL_NAMES),
                ).fetchall()
            ]
            if entity not in actor_tool_use_ids:
                return 0
            idx = actor_tool_use_ids.index(entity)
            event_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM run_events WHERE turn_id = ? ORDER BY id ASC",
                    (turn_id,),
                ).fetchall()
            ]
            if idx >= len(event_ids):
                return 0
            cur = conn.execute(
                "UPDATE run_events SET outcome_tag = ?, source = 'manual' WHERE id = ?",
                (outcome, event_ids[idx]),
            )
            return cur.rowcount
        cur = conn.execute(
            "UPDATE run_events SET outcome_tag = ?, source = 'manual' WHERE turn_id = ?",
            (outcome, turn_id),
        )
        return cur.rowcount


def backfill_all(conn: sqlite3.Connection, *, log=print) -> dict:
    """Populate run_events for every existing turn that has actor tool_calls
    but no run_events rows yet. Idempotent (derive_for_turn deletes-then-
    inserts per turn). Returns a summary dict."""
    placeholders = _in_placeholders(RUN_TOOL_NAMES)
    rows = conn.execute(
        f"""
        SELECT DISTINCT t.id AS turn_id, t.session_id, t.cwd
        FROM turns t
        JOIN tool_calls tc ON tc.turn_id = t.id
        WHERE tc.tool_name IN ({placeholders})
        ORDER BY t.id ASC
        """,
        tuple(RUN_TOOL_NAMES),
    ).fetchall()

    total = len(rows)
    inserted = 0
    tagged = 0
    for i, r in enumerate(rows, 1):
        n = derive_for_turn(
            conn, r["turn_id"], r["session_id"],
            project=_project_from_cwd(r["cwd"]),
            source="backfill",
            tool_calls=None,  # read from DB
        )
        inserted += n
        tagged += conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE turn_id = ? AND outcome_tag IS NOT NULL",
            (r["turn_id"],),
        ).fetchone()[0]
        if i % 500 == 0 or i == total:
            log(f"  run_events backfill {i}/{total}  rows={inserted}  tagged={tagged}")
    return {"turns": total, "rows": inserted, "tagged": tagged}


# ─── Aggregate recipes (consumed by bin/run-stats + charts) ──────────────

def command_success_rate(conn: sqlite3.Connection) -> dict:
    """Current command success-vs-fail counts + rate.

    Reports FOUR quantities so the denominator bias is honest:
      - total           : all run_events (Bash tool_calls)
      - resolved        : run_events whose outcome_tag matched a rule
      - unresolved      : run_events with NULL outcome_tag (regex didn't match)
      - resolution_rate  : resolved / total  — the headline coverage metric
      - success / failure / rate : over RESOLVED outcomes only — this is
        P(success | resolved), NOT overall command success. Labeled
        conditional in the CLI. NULLs are missing-not-at-random (resolution
        depends on tool verbosity, not on what happened), so this rate
        cannot be read as the true success rate.
      - bounds : worst/best-case overall success if every unresolved run_event
        were a failure / a success — the uncertainty band from the NULLs.
    """
    cfg = outcome_rules.load()["projects"]["__default__"]
    rules = cfg.get("outcome_rules", outcome_rules.DEFAULT_OUTCOME_RULES)
    success_tags = {r["tag"] for r in rules if r.get("outcome") == "success"}
    failure_tags = {r["tag"] for r in rules if r.get("outcome") == "failure"}

    total = conn.execute("SELECT COUNT(*) AS n FROM run_events").fetchone()["n"]
    rows = conn.execute(
        "SELECT outcome_tag, COUNT(*) AS n FROM run_events "
        "WHERE outcome_tag IS NOT NULL GROUP BY outcome_tag"
    ).fetchall()
    s = f = 0
    tag_counts: dict[str, int] = {}
    for r in rows:
        tag_counts[r["outcome_tag"]] = int(r["n"])
        if r["outcome_tag"] in success_tags:
            s += int(r["n"])
        elif r["outcome_tag"] in failure_tags:
            f += int(r["n"])
    resolved = s + f
    unresolved = int(total) - resolved
    rate = (s / resolved) if resolved else None
    # Worst case: all unresolved are failures. Best case: all successes.
    worst = (s / int(total)) if total else None
    best = ((s + unresolved) / int(total)) if total else None
    return {
        "total": int(total),
        "resolved": resolved,
        "unresolved": unresolved,
        "resolution_rate": (resolved / int(total)) if total else None,
        "success": s,
        "failure": f,
        "rate": rate,  # conditional on resolution
        "bounds": {"worst": worst, "best": best},
        "tag_counts": tag_counts,
    }


def outcome_trend(conn: sqlite3.Connection, *, bucket: str = "week") -> list[dict]:
    """Per-bucket success / failure / unresolved counts. bucket = 'day' | 'week'.

    `unresolved` is included so the dashboard shows the NULL fraction over
    time — without it the success% line hides that 81% of run_events carry no
    resolved outcome, and a viewer would read success% as overall success."""
    cfg = outcome_rules.load()["projects"]["__default__"]
    rules = cfg.get("outcome_rules", outcome_rules.DEFAULT_OUTCOME_RULES)
    success_tags = {r["tag"] for r in rules if r.get("outcome") == "success"}
    failure_tags = {r["tag"] for r in rules if r.get("outcome") == "failure"}
    fmt = "%Y-W%W" if bucket == "week" else "%Y-%m-%d"
    # Build the IN(...) placeholder lists BEFORE the f-string is assembled so
    # no old-style `%` interpolation ever runs on a string that already
    # contains the literal `%Y` / `%W` / `%m` / `%d` from strftime's format
    # spec — doing `"""...""" % {...}` on top of that collided with the
    # literal percent-escapes and raised
    # `ValueError: unsupported format character 'Y'`.
    succ_placeholders = _in_placeholders(success_tags)
    fail_placeholders = _in_placeholders(failure_tags)
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', re.started_at) AS b,
               CASE WHEN re.outcome_tag IS NULL THEN '__unresolved__'
                    WHEN re.outcome_tag IN ({succ_placeholders}) THEN 'success'
                    WHEN re.outcome_tag IN ({fail_placeholders}) THEN 'failure'
                    ELSE 'other' END AS kind,
               COUNT(*) AS n
        FROM run_events re
        WHERE re.started_at IS NOT NULL
        GROUP BY b, kind
        ORDER BY b
        """,
        (*success_tags, *failure_tags),
    ).fetchall() if (success_tags or failure_tags) else []
    # Fallback when the rule set has no success or failure tags (shouldn't
    # happen with the shipped defaults, but don't crash on an empty config).
    if not rows:
        rows = conn.execute(
            f"""
            SELECT strftime('{fmt}', re.started_at) AS b,
                   CASE WHEN re.outcome_tag IS NULL THEN '__unresolved__'
                        ELSE re.outcome_tag END AS kind,
                   COUNT(*) AS n
            FROM run_events re
            WHERE re.started_at IS NOT NULL
            GROUP BY b, kind ORDER BY b
            """
        ).fetchall()
    buckets: dict[str, dict] = {}
    for r in rows:
        b = r["b"]
        if b not in buckets:
            buckets[b] = {"bucket": b, "success": 0, "failure": 0, "unresolved": 0}
        k = r["kind"]
        if k == "success":
            buckets[b]["success"] += int(r["n"])
        elif k == "failure":
            buckets[b]["failure"] += int(r["n"])
        elif k == "__unresolved__":
            buckets[b]["unresolved"] += int(r["n"])
    return list(buckets.values())
