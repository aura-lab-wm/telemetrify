"""Health-check & diagnostic report for the prompt-telemetry pipeline.

Exposes:
- `run_health_checks() -> dict`     : machine-readable report.
- `format_report(report) -> str`    : pretty-printed terminal output (ANSI color).
- `main()`                          : argparse CLI, exits 0 if healthy else 1.

Used by `bin/doctor` and by the `/api/health` FastAPI route.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import DATA_DIR, DB_PATH, LOG_PATH
from .db import connect

# Threshold knobs — flipping these flips overall `healthy`.
LAG_MAX_MINUTES = 24 * 60
VECTOR_COVERAGE_MIN_PCT = 95.0
RECENT_ERRORS_TAIL_LINES = 5
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CAPTURE_HOOK_PATH = Path(__file__).resolve().parents[2] / "bin" / "capture-hook"

# ANSI color codes — no rich library dependency.
_RESET = "\033[0m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------

def _schema_version(conn) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _last_capture(conn) -> tuple[str | None, float | None]:
    """Returns (iso_ts, lag_minutes) for the most recent hook-source ingest."""
    row = conn.execute(
        "SELECT MAX(started_at) AS t FROM ingest_runs WHERE source = 'hook'"
    ).fetchone()
    last = row["t"] if row else None
    if not last:
        return None, None
    try:
        # tolerate both `Z` and `+00:00` suffix variants.
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return last, round(delta.total_seconds() / 60.0, 2)
    except ValueError:
        return last, None


def _counts(conn) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, sql in (
        ("sessions", "SELECT COUNT(*) AS c FROM sessions"),
        ("turns", "SELECT COUNT(*) AS c FROM turns"),
        ("tool_calls", "SELECT COUNT(*) AS c FROM tool_calls"),
        ("reruns", "SELECT COUNT(*) AS c FROM reruns"),
    ):
        try:
            out[key] = int(conn.execute(sql).fetchone()["c"])
        except Exception:
            out[key] = 0
    return out


def _pct(numer: int, denom: int) -> float:
    if denom <= 0:
        return 0.0
    return round((numer / denom) * 100.0, 2)


def _coverage(conn, turns_total: int) -> dict[str, float]:
    """Coverage percentages across vec/prompt_vec/fts/cluster tables."""
    def _count(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()["c"])
        except Exception:
            return 0

    return {
        "vector_coverage_pct": _pct(_count("SELECT COUNT(*) AS c FROM turn_vec"), turns_total),
        "prompt_vector_coverage_pct": _pct(_count("SELECT COUNT(*) AS c FROM prompt_vec"), turns_total),
        "fts_coverage_pct": _pct(_count("SELECT COUNT(*) AS c FROM turns_fts"), turns_total),
        "cluster_coverage_pct": _pct(_count("SELECT COUNT(*) AS c FROM turn_cluster"), turns_total),
    }


def _captures_last_24h(conn) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM ingest_runs
        WHERE started_at >= datetime('now', '-1 day')
        """
    ).fetchone()
    return int(row["c"]) if row else 0


def _recent_errors() -> list[str]:
    """Tail capture.log for the last N error-ish lines."""
    if not LOG_PATH.exists():
        return []
    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    needles = ("failed", "Traceback")
    matches = [ln.rstrip("\n") for ln in lines if any(n in ln for n in needles)]
    return matches[-RECENT_ERRORS_TAIL_LINES:]


def _hook_wired() -> bool:
    """Confirm settings.json has a Stop hook whose command resolves to capture-hook."""
    if not SETTINGS_PATH.exists():
        return False
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    stop_blocks = data.get("hooks", {}).get("Stop", [])
    if not isinstance(stop_blocks, list):
        return False
    target = str(CAPTURE_HOOK_PATH.resolve())
    for block in stop_blocks:
        for hook in (block or {}).get("hooks", []) or []:
            cmd = (hook or {}).get("command", "")
            if not cmd:
                continue
            # match either the exact absolute path or any command string containing
            # the canonical `bin/capture-hook` suffix.
            try:
                if str(Path(cmd).resolve()) == target:
                    return True
            except OSError:
                pass
            if cmd.endswith("bin/capture-hook"):
                return True
    return False


def _file_mb(p: Path) -> float:
    try:
        return round(p.stat().st_size / (1024 * 1024), 2)
    except OSError:
        return 0.0


def _disk_free_mb() -> float:
    try:
        usage = shutil.disk_usage(str(DATA_DIR))
        return round(usage.free / (1024 * 1024), 2)
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_health_checks() -> dict[str, Any]:
    conn = connect()
    counts = _counts(conn)
    turns_total = counts["turns"]
    last_capture_at, lag_minutes = _last_capture(conn)
    cov = _coverage(conn, turns_total)
    report: dict[str, Any] = {
        "schema_version": _schema_version(conn),
        "last_capture_at": last_capture_at,
        "lag_minutes": lag_minutes,
        **counts,
        **cov,
        "captures_last_24h": _captures_last_24h(conn),
        "recent_errors": _recent_errors(),
        "hook_wired": _hook_wired(),
        "db_size_mb": _file_mb(DB_PATH),
        "wal_size_mb": _file_mb(Path(str(DB_PATH) + "-wal")),
        "disk_free_mb": _disk_free_mb(),
    }

    fail_reasons: list[str] = []
    if not report["hook_wired"]:
        fail_reasons.append("hook_not_wired")
    if lag_minutes is None or lag_minutes > LAG_MAX_MINUTES:
        fail_reasons.append("lag_exceeds_24h")
    if report["vector_coverage_pct"] < VECTOR_COVERAGE_MIN_PCT:
        fail_reasons.append("vector_coverage_below_threshold")
    if report["recent_errors"]:
        fail_reasons.append("recent_capture_errors")

    report["healthy"] = not fail_reasons
    report["fail_reasons"] = fail_reasons
    return report


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def _glyph(level: str) -> str:
    if level == "ok":
        return f"{_GREEN}✓{_RESET}"
    if level == "warn":
        return f"{_YELLOW}⚠{_RESET}"
    return f"{_RED}✗{_RESET}"


def _row(level: str, label: str, value: str) -> str:
    return f"  {_glyph(level)} {label:<28} {value}"


def format_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    overall = "ok" if report["healthy"] else "fail"
    header_color = _GREEN if report["healthy"] else _RED
    status_word = "HEALTHY" if report["healthy"] else "UNHEALTHY"
    lines.append(f"{_BOLD}prompt-telemetry doctor{_RESET}  "
                 f"{header_color}[{status_word}]{_RESET}")
    lines.append("")

    # Wiring & capture freshness ------------------------------------------------
    lines.append(f"{_BOLD}Wiring & capture{_RESET}")
    lines.append(_row(
        "ok" if report["hook_wired"] else "fail",
        "Stop hook wired",
        "yes" if report["hook_wired"] else f"no  ({CAPTURE_HOOK_PATH})",
    ))
    last = report["last_capture_at"] or "never"
    lag = report["lag_minutes"]
    if lag is None:
        lag_level, lag_str = "fail", "n/a (no captures recorded)"
    elif lag > LAG_MAX_MINUTES:
        lag_level, lag_str = "fail", f"{lag:.1f} min ({lag / 60:.1f}h, stale)"
    elif lag > 60:
        lag_level, lag_str = "warn", f"{lag:.1f} min ({lag / 60:.1f}h)"
    else:
        lag_level, lag_str = "ok", f"{lag:.1f} min"
    lines.append(_row(lag_level, "Last capture", str(last)))
    lines.append(_row(lag_level, "Lag", lag_str))
    lines.append(_row("ok", "Captures last 24h", str(report["captures_last_24h"])))
    lines.append("")

    # Schema & counts -----------------------------------------------------------
    lines.append(f"{_BOLD}Schema & counts{_RESET}")
    lines.append(_row("ok", "Schema version", str(report["schema_version"])))
    lines.append(_row("ok", "Sessions", f"{report['sessions']:,}"))
    lines.append(_row("ok", "Turns", f"{report['turns']:,}"))
    lines.append(_row("ok", "Tool calls", f"{report['tool_calls']:,}"))
    lines.append(_row("ok", "Reruns", f"{report['reruns']:,}"))
    lines.append("")

    # Coverage ------------------------------------------------------------------
    lines.append(f"{_BOLD}Index coverage{_RESET}")

    def _cov_row(label: str, pct: float, threshold: float | None = None) -> str:
        if threshold is not None:
            level = "ok" if pct >= threshold else "fail"
        else:
            level = "ok" if pct >= 95 else ("warn" if pct >= 60 else "fail")
        return _row(level, label, f"{pct:.1f}%")

    lines.append(_cov_row("Full-turn vectors", report["vector_coverage_pct"], VECTOR_COVERAGE_MIN_PCT))
    lines.append(_cov_row("Prompt-only vectors", report["prompt_vector_coverage_pct"]))
    lines.append(_cov_row("FTS index", report["fts_coverage_pct"]))
    lines.append(_cov_row("Cluster assignments", report["cluster_coverage_pct"]))
    lines.append("")

    # Storage -------------------------------------------------------------------
    lines.append(f"{_BOLD}Storage{_RESET}")
    lines.append(_row("ok", "prompts.db", f"{report['db_size_mb']:.1f} MB"))
    lines.append(_row("ok" if report["wal_size_mb"] < 256 else "warn",
                      "prompts.db-wal", f"{report['wal_size_mb']:.1f} MB"))
    disk = report["disk_free_mb"]
    disk_level = "ok" if disk >= 1024 else ("warn" if disk >= 256 else "fail")
    lines.append(_row(disk_level, "Disk free (data dir)", f"{disk:.0f} MB"))
    lines.append("")

    # Errors --------------------------------------------------------------------
    lines.append(f"{_BOLD}Recent capture errors{_RESET}  ({LOG_PATH})")
    errs = report["recent_errors"]
    if not errs:
        lines.append(f"  {_glyph('ok')} none")
    else:
        for err in errs:
            lines.append(f"  {_glyph('fail')} {_DIM}{err}{_RESET}")
    lines.append("")

    # Footer --------------------------------------------------------------------
    if report["healthy"]:
        lines.append(f"{_GREEN}All checks passed.{_RESET}")
    else:
        reasons = ", ".join(report["fail_reasons"]) or "see above"
        lines.append(f"{_RED}Failed: {reasons}{_RESET}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prompt-telemetry-doctor",
        description="Health-check the prompt-telemetry pipeline.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Dump JSON instead of the pretty-printed terminal report.",
    )
    args = parser.parse_args(argv)
    report = run_health_checks()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
