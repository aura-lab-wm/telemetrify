"""v18: run_events ledger + the evidence_backed auto_grades dimension.

The performance backbone — one row per captured tool_call that "does work"
(Bash first; extensible), denormalizing session_id + tool_name so analysis
never needs the hand-join through turns/tool_calls that silently no-ops
(tool_calls has NO session_id column — that hand-join has caused real
analysis bugs). exit_code / duration_ms are NULL where not derivable from
the captured record; we never fabricate them.

outcome_tag is written by the outcome-stamping layer from CONFIG-DRIVEN,
per-project regex rules matched against OUTPUT ONLY (tool_calls.output_text
/ program stdout) — never against input_json or a file's contents. A
success marker that appears only in source code the agent READ/edited must
never be counted as an outcome (the real bug that motivated this).

`source` records how the row's outcome was determined:
  'auto-output-match' — regex matched the tool result output
  'manual'            — set via `telemetrify tag` CLI
  'capture'           — row created at capture time, no outcome matched
  'backfill'          — row created via backfill, no outcome matched

This is a .py migration (not .sql) so the additive `evidence_backed` column
on auto_grades can be guarded with a PRAGMA table_info precheck — SQLite
has no IF-NOT-EXISTS for ADD COLUMN. The table + indexes use
CREATE … IF NOT EXISTS so a re-apply is a no-op even without the ledger
gate. BACKUP_FIRST is set per the operator's standing rule: back up the DB
before any migration that touches the live schema.
"""
import sqlite3

BACKUP_FIRST = True


def up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS run_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id      INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
            session_id   TEXT NOT NULL,
            tool_name    TEXT NOT NULL,
            command      TEXT,
            exit_code    INTEGER,
            duration_ms  INTEGER,
            outcome_tag  TEXT,
            source       TEXT NOT NULL,
            started_at   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_run_events_turn    ON run_events(turn_id);
        CREATE INDEX IF NOT EXISTS idx_run_events_session ON run_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_run_events_started ON run_events(started_at);
        CREATE INDEX IF NOT EXISTS idx_run_events_tool     ON run_events(tool_name);
        CREATE INDEX IF NOT EXISTS idx_run_events_outcome ON run_events(outcome_tag);
        CREATE INDEX IF NOT EXISTS idx_run_events_source  ON run_events(source);
        """
    )

    cols = {row[1] for row in conn.execute("PRAGMA table_info(auto_grades)").fetchall()}
    if "evidence_backed" not in cols:
        # 1 = success claim in assistant_text is backed by a tool RESULT
        # 0 = a success claim is present but no tool result backs it (unsupported)
        # NULL = no success claim was made / not assessable
        conn.execute("ALTER TABLE auto_grades ADD COLUMN evidence_backed INTEGER")

    # Index the new dimension so the per-day / per-session aggregate that
    # drives the unsupported-claim-rate chart stays cheap as the corpus grows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auto_grades_evidence ON auto_grades(evidence_backed)"
    )