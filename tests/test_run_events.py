"""run_events ledger + outcome stamping + evidence-backing tests.

Covers the three subsystems the operator spec'd:
  1) run_events population (capture-time + backfill), output-only outcome
     matching, the read-source anti-false-positive.
  2) evidence-backing grade (unsupported-claim rate).
  3) outcome-stamping layer + `telemetrify tag` CLI.
"""
from __future__ import annotations

import json

import pytest

from telemetrify import outcome_rules
from telemetrify.evidence import assess_turn, unsupported_claim_rate
from telemetrify.run_events import (
    backfill_all,
    command_success_rate,
    derive_for_turn,
    manual_tag,
    outcome_trend,
    stamp_outcomes_for_turn,
)


# ─── fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def turn_with_bash(migrated_db):
    """Insert one session + one turn with two Bash tool_calls: a success
    (`tests passed`, exit 0) and a failure (`error:`, exit 1). Returns
    (conn, turn_id, session_id)."""
    conn = migrated_db
    conn.execute(
        "INSERT INTO sessions(id, started_at, cwd) VALUES (?, ?, ?)",
        ("sess-1", "2026-07-01T00:00:00Z", "/Users/amastro/Projects/foo"),
    )
    cur = conn.execute(
        """
        INSERT INTO turns(session_id, user_text, assistant_text, started_at, cwd)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("sess-1", "run tests", "tests passed.", "2026-07-01T00:01:00Z",
         "/Users/amastro/Projects/foo"),
    )
    turn_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO tool_calls(turn_id, seq, tool_name, tool_use_id, input_json, output_text, is_error, started_at)
        VALUES (?, 1, 'Bash', ?, ?, ?, 0, ?)
        """,
        (turn_id, "tu_ok", json.dumps({"command": "pytest"}),
         "5 passed, 0 failed\nexit code 0", "2026-07-01T00:01:01Z"),
    )
    conn.execute(
        """
        INSERT INTO tool_calls(turn_id, seq, tool_name, tool_use_id, input_json, output_text, is_error, started_at)
        VALUES (?, 2, 'Bash', ?, ?, ?, 1, ?)
        """,
        (turn_id, "tu_fail", json.dumps({"command": "make"}),
         "error: expected ';' at main.c:12\nexit code 2", "2026-07-01T00:01:05Z"),
    )
    # A Read of a file that CONTAINS a success marker — must NOT count as
    # an outcome (the read-source anti-false-positive that motivated this).
    conn.execute(
        """
        INSERT INTO tool_calls(turn_id, seq, tool_name, tool_use_id, input_json, output_text, is_error, started_at)
        VALUES (?, 3, 'Read', ?, ?, ?, 0, ?)
        """,
        (turn_id, "tu_read", json.dumps({"file_path": "src/x.py"}),
         "# note: ██████ CREATED (replayed hold!)\nprint('hi')",
         "2026-07-01T00:01:02Z"),
    )
    conn.commit()
    return conn, turn_id, "sess-1"


# ─── #1 run_events population ────────────────────────────────────────────

def test_derive_creates_one_row_per_bash_and_denormalizes(turn_with_bash):
    conn, turn_id, session_id = turn_with_bash
    from telemetrify.transcript import ToolCall
    # Simulate the capture path: pass in-memory ToolCall objects.
    tcs = [
        ToolCall(seq=1, tool_name="Bash", tool_use_id="tu_ok",
                 input_json=json.dumps({"command": "pytest"}), output_text="5 passed, 0 failed",
                 is_error=False, started_at="2026-07-01T00:01:01Z"),
        ToolCall(seq=2, tool_name="Bash", tool_use_id="tu_fail",
                 input_json=json.dumps({"command": "make"}), output_text="error: x",
                 is_error=True, started_at="2026-07-01T00:01:05Z"),
    ]
    n = derive_for_turn(conn, turn_id, session_id, project="/Users/amastro/Projects/foo",
                        source="capture", tool_calls=tcs)
    assert n == 2
    rows = conn.execute(
        "SELECT tool_name, session_id, command, outcome_tag, source FROM run_events "
        "WHERE turn_id = ? ORDER BY id", (turn_id,)
    ).fetchall()
    assert len(rows) == 2
    # session_id + tool_name denormalized (no hand-join needed)
    assert all(r["session_id"] == "sess-1" for r in rows)
    assert all(r["tool_name"] == "Bash" for r in rows)
    # command extracted from input_json
    cmds = [r["command"] for r in rows]
    assert "pytest" in cmds and "make" in cmds
    # exit_code / duration_ms are NULL (not derivable — never fabricated)
    nulls = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE exit_code IS NOT NULL OR duration_ms IS NOT NULL"
    ).fetchone()[0]
    assert nulls == 0


def test_outcome_matched_against_output_only_not_read_source(turn_with_bash):
    """The critical discipline: a success marker that appears only in source
    code the agent READ must never become an outcome_tag."""
    conn, turn_id, session_id = turn_with_bash
    derive_for_turn(conn, turn_id, session_id, project="foo",
                   source="capture", tool_calls=None)  # backfill path reads from DB
    tags = [r["outcome_tag"] for r in conn.execute(
        "SELECT outcome_tag FROM run_events WHERE turn_id = ? ORDER BY id", (turn_id,)
    ).fetchall()]
    # The Read tool's output ("████ CREATED (replayed hold!)") is NOT a
    # run_event at all (Read isn't an actor tool), and even if it were, its
    # contents must not produce a 'committed'/'command_ok' tag.
    assert "committed" not in tags
    # The pytest run resolves to a success outcome; the make run to a failure.
    assert "tests_passed" in tags or "command_ok" in tags
    assert "build_failed" in tags or "command_error" in tags


def test_backfill_is_idempotent(turn_with_bash):
    conn, turn_id, _ = turn_with_bash
    s1 = backfill_all(conn, log=lambda *_: None)
    s2 = backfill_all(conn, log=lambda *_: None)
    # Second pass must not double-count rows.
    assert s1["turns"] == s2["turns"] == 1
    assert s1["rows"] == s2["rows"]  # both 2 (deleted then re-inserted)
    n = conn.execute("SELECT COUNT(*) FROM run_events WHERE turn_id = ?", (turn_id,)).fetchone()[0]
    assert n == 2


def test_capture_path_insert_turn_joins_caller_transaction(migrated_db):
    """store.insert_turn must populate run_events WITHOUT committing early
    (the nested-`with conn:` anti-pattern that flushes a partial turn and
    defeats the caller's rollback). Verified by forcing a rollback after
    insert_turn and confirming run_events is NOT persisted."""
    from telemetrify.store import upsert_session, insert_turn
    from telemetrify.transcript import Turn, ToolCall
    conn = migrated_db
    turn = Turn(
        session_id="sess-x", user_uuid="u1", parent_uuid=None, prompt_id=None,
        user_text="run it", assistant_text="done",
        thinking_text="", model=None, started_at="2026-07-01T00:00:00Z",
        finished_at=None, latency_ms=None, input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        cwd="/proj", git_branch=None, project_dir=None, transcript_path=None,
        cli_version=None, entrypoint=None, user_type=None,
        attribution_skill=None, attribution_plugin=None,
        tool_call_count=1, assistant_message_count=1,
        tool_calls=[ToolCall(seq=1, tool_name="Bash", tool_use_id="t1",
                             input_json='{"command": "echo hi"}',
                             output_text="hi\nexit code 0", is_error=False,
                             started_at="2026-07-01T00:00:01Z")],
        raw_json="",
    )
    with conn:
        upsert_session(conn, turn)
        tid = insert_turn(conn, turn, embedding=None)
    assert tid is not None
    # run_events written as part of the caller's committed transaction.
    assert conn.execute("SELECT COUNT(*) FROM run_events WHERE turn_id = ?", (tid,)).fetchone()[0] == 1

    # Now simulate a failure AFTER insert_turn inside the same transaction →
    # the run_events rows must roll back with the turn (no early commit).
    turn2 = Turn(**{**turn.__dict__, "user_uuid": "u2", "user_text": "again"})
    try:
        with conn:
            upsert_session(conn, turn2)
            tid2 = insert_turn(conn, turn2, embedding=None)
            assert tid2 is not None
            # run_events visible mid-transaction (uncommitted read on same conn)
            assert conn.execute("SELECT COUNT(*) FROM run_events WHERE turn_id = ?", (tid2,)).fetchone()[0] == 1
            raise RuntimeError("simulated downstream failure in capture.py")
    except RuntimeError:
        pass
    # After rollback, tid2's run_events must be gone (proves no early commit).
    assert conn.execute("SELECT COUNT(*) FROM run_events WHERE turn_id = ?", (tid2,)).fetchone()[0] == 0
    # And the turn itself rolled back (the whole point — no partial flush).
    assert conn.execute("SELECT COUNT(*) FROM turns WHERE id = ?", (tid2,)).fetchone()[0] == 0


def test_command_success_rate_reports_resolution_and_bounds(turn_with_bash):
    """The headline is the resolution rate + conditional success + bounds —
    not a bare 'success rate' that hides the 81%-NULL denominator bias."""
    conn, turn_id, _ = turn_with_bash
    derive_for_turn(conn, turn_id, "sess-1", project="foo", source="capture",
                    tool_calls=None)
    r = command_success_rate(conn)
    assert r["success"] >= 1
    assert r["failure"] >= 1
    assert r["rate"] is not None and 0.0 <= r["rate"] <= 1.0  # conditional on resolution
    # total + resolution coverage reported honestly
    assert r["total"] >= r["resolved"]
    assert r["unresolved"] == r["total"] - r["resolved"]
    assert r["resolution_rate"] is not None and 0.0 <= r["resolution_rate"] <= 1.0
    # bounds bracket the conditional rate
    assert r["bounds"]["worst"] <= r["rate"] <= r["bounds"]["best"]


def test_outcome_trend_categorizes_when_only_success_tags_configured(turn_with_bash, monkeypatch):
    """Regression: a rule set with success tags but NO failure tags (or vice
    versa) must still run the categorized query instead of silently falling
    back to the raw-tag query, whose output the aggregation loop drops for
    any tag other than 'success'/'failure'/'__unresolved__' (previously this
    made the chart report zero success/failure activity for a project that
    actually had some)."""
    conn, turn_id, _ = turn_with_bash
    # Only success rules configured — no failure tags at all.
    monkeypatch.setattr(outcome_rules, "load", lambda: {
        "projects": {
            "__default__": {
                "outcome_rules": [
                    {"tag": "tests_passed", "outcome": "success", "pattern": r"passed"},
                ],
            },
        },
    })
    derive_for_turn(conn, turn_id, "sess-1", project="foo", source="capture",
                    tool_calls=None)
    trend = outcome_trend(conn)
    assert trend  # must not be the empty-list fallback
    assert sum(b["success"] for b in trend) >= 1
    assert sum(b["failure"] for b in trend) == 0


# ─── #2 evidence-backing grade ───────────────────────────────────────────

@pytest.fixture
def claimed_turn(migrated_db):
    """A turn that asserts 'created' with a backing Bash result, plus a
    second turn asserting 'done' with no backing tool result."""
    conn = migrated_db
    conn.execute(
        "INSERT INTO sessions(id, started_at, cwd) VALUES (?, ?, ?)",
        ("sess-a", "2026-07-02T00:00:00Z", "/proj"),
    )
    # backed turn
    cur = conn.execute(
        "INSERT INTO turns(session_id, user_text, assistant_text, started_at, cwd) "
        "VALUES (?, 'create the file', ?, ?, ?)",
        ("sess-a", "I created the file.", "2026-07-02T00:01:00Z", "/proj"),
    )
    t1 = cur.lastrowid
    conn.execute(
        "INSERT INTO tool_calls(turn_id, seq, tool_name, tool_use_id, input_json, output_text, is_error, started_at) "
        "VALUES (?, 1, 'Bash', ?, ?, ?, 0, ?)",
        (t1, "tu1", json.dumps({"command": "touch x.py"}), "exit code 0", "2026-07-02T00:01:01Z"),
    )
    # unsupported turn — claims 'done' but only has a Read (read-only, can't back)
    cur = conn.execute(
        "INSERT INTO turns(session_id, user_text, assistant_text, started_at, cwd) "
        "VALUES (?, 'fix the bug', ?, ?, ?)",
        ("sess-a", "done, fixed it.", "2026-07-02T00:02:00Z", "/proj"),
    )
    t2 = cur.lastrowid
    conn.execute(
        "INSERT INTO tool_calls(turn_id, seq, tool_name, tool_use_id, input_json, output_text, is_error, started_at) "
        "VALUES (?, 1, 'Read', ?, ?, ?, 0, ?)",
        (t2, "tu2", json.dumps({"file_path": "y.py"}),
         "# looks done to me\nexit code 0", "2026-07-02T00:02:01Z"),
    )
    conn.commit()
    return conn, t1, t2


def test_evidence_backs_claim_when_bash_success(claimed_turn):
    conn, t1, t2 = claimed_turn
    assert assess_turn(conn, t1) == 1  # claim + backing Bash result


def test_evidence_unsupported_when_only_read_backs(claimed_turn):
    """A Read tool's output containing 'exit code 0' must NOT back the claim
    — read-only tools can't evidence an outcome (the read-source bug)."""
    conn, t1, t2 = claimed_turn
    assert assess_turn(conn, t2) == 0  # claim but only a Read result


def test_evidence_no_claim_is_null(claimed_turn):
    conn, t1, t2 = claimed_turn
    cur = conn.execute(
        "INSERT INTO turns(session_id, user_text, assistant_text, started_at, cwd) "
        "VALUES ('sess-a', 'q', 'maybe it could be a thing.', '2026-07-02T00:03:00Z', '/proj')"
    )
    t3 = cur.lastrowid
    assert assess_turn(conn, t3) is None  # hedged, no claim


def test_unsupported_claim_rate(claimed_turn):
    conn, t1, t2 = claimed_turn
    assess_turn(conn, t1)
    assess_turn(conn, t2)
    r = unsupported_claim_rate(conn)
    assert r["backed"] == 1
    assert r["unsupported"] == 1
    assert r["rate"] == 0.5


# ─── #3 outcome-stamping + tag CLI ───────────────────────────────────────

def test_manual_tag_via_cli(turn_with_bash, monkeypatch):
    conn, turn_id, _ = turn_with_bash
    derive_for_turn(conn, turn_id, "sess-1", project="foo", source="capture",
                    tool_calls=None)
    # tag_cli opens its own connection via telemetrify.db.connect; point it
    # at the fixture DB so the CLI sees the fixture's turn.
    import telemetrify.tag_cli as tag_cli_mod
    monkeypatch.setattr(tag_cli_mod, "connect", lambda: conn)
    rc = tag_cli_mod.main(["--turn", str(turn_id), "--outcome", "manual_pass"])
    assert rc == 0
    tags = [r["outcome_tag"] for r in conn.execute(
        "SELECT outcome_tag FROM run_events WHERE turn_id = ?", (turn_id,)
    ).fetchall()]
    assert all(t == "manual_pass" for t in tags)
    srcs = [r["source"] for r in conn.execute(
        "SELECT source FROM run_events WHERE turn_id = ?", (turn_id,)
    ).fetchall()]
    assert all(s == "manual" for s in srcs)


def test_auto_restamp_from_output(turn_with_bash):
    conn, turn_id, _ = turn_with_bash
    # First seed with no outcome by tagging manually then re-deriving auto.
    derive_for_turn(conn, turn_id, "sess-1", project="foo", source="capture",
                    tool_calls=None)
    n = stamp_outcomes_for_turn(conn, turn_id, source="auto-output-match")
    assert n >= 1
    srcs = [r["source"] for r in conn.execute(
        "SELECT source FROM run_events WHERE turn_id = ? AND outcome_tag IS NOT NULL",
        (turn_id,)
    ).fetchall()]
    assert all(s == "auto-output-match" for s in srcs)


def test_init_config_writes_default_file(tmp_path, monkeypatch):
    import telemetrify as pkg
    monkeypatch.setattr(pkg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(outcome_rules, "DATA_DIR", tmp_path)
    monkeypatch.setattr(outcome_rules, "CONFIG_PATH", tmp_path / "outcome_rules.json")
    from telemetrify.tag_cli import main as tag_main
    rc = tag_main(["--init-config"])
    assert rc == 0
    cfg = json.loads((tmp_path / "outcome_rules.json").read_text())
    assert "projects" in cfg and "__default__" in cfg["projects"]