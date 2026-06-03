"""telemetrify.rocco_sync — replicate the local SQLite corpus to Rocco.

The local DB (`data/prompts.db`) is telemetrify's "generous local cache": every
captured turn lands there first. This module copies that whole DB up to the
Rocco GPU box's persistent storage so the corpus survives a Mac wipe and is
reachable from Rocco-side tooling. **Copy semantics, never move** — the local
DB is never modified or deleted; Rocco is a durable replica.

Push triggers (evaluated each `tick`, driven by a 5-min launchd agent):
  • bootstrap  — first ever run, no prior push → seed the remote.
  • retry      — the previous push failed → try again while connected.
  • reconnect  — connectivity returned after an outage (prev_connected False
                 → now True): flush the backlog accumulated during the outage.
  • active-2h  — connection healthy AND ≥2h of *cumulative active session time*
                 (Σ per-session max(finished_at)-min(started_at)) has elapsed
                 since the last successful push.

`restore` is the reverse direction, used only for disaster recovery: if the
local DB is missing/empty on startup, pull the snapshot back down from Rocco so
general ("whole-corpus") queries have history to search. It refuses to clobber a
present local DB unless `--force`.

Transport: `rsync -a --partial --inplace` over ssh to `$ROCCO_SSH_HOST`
(default "rocco") at `$ROCCO_STORE_DIR` (default "~/telemetrify-store"). rsync's
block-delta keeps repeat pushes cheap even though the DB is ~1.4 GB.

Every ssh/rsync call funnels through the single `_run` seam so tests can stub
the network entirely.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import DATA_DIR, DB_PATH

# 2h of cumulative active session time triggers a healthy-connection flush.
THRESHOLD_S = 2 * 3600

STATE_PATH = DATA_DIR / "rocco-sync-state.json"
SNAPSHOT_PATH = DATA_DIR / ".rocco-snapshot" / "prompts.db"


class RoccoSyncError(RuntimeError):
    """A push/restore step failed (mkdir/rsync non-zero, etc.)."""


# ── config (env-driven) ──────────────────────────────────────────────────
def ssh_host() -> str:
    return os.environ.get("ROCCO_SSH_HOST", "rocco")


def store_dir() -> str:
    return os.environ.get("ROCCO_STORE_DIR", "~/telemetrify-store").rstrip("/")


def _ssh_opts() -> list[str]:
    return os.environ.get(
        "ROCCO_SSH_OPTS", "-o BatchMode=yes -o ConnectTimeout=4").split()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _remote_target() -> str:
    return f"{ssh_host()}:{store_dir()}/prompts.db"


# ── subprocess seam (tests monkeypatch this) ─────────────────────────────
def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ── persistent state ─────────────────────────────────────────────────────
@dataclass
class SyncState:
    last_push_finished_at: str | None = None
    last_push_ok: bool = False
    active_seconds_at_last_push: int = 0
    prev_connected: bool | None = None


def load_state(path: Path | None = None) -> SyncState:
    p = Path(path) if path is not None else STATE_PATH
    try:
        d = json.loads(p.read_text())
    except Exception:
        return SyncState()
    return SyncState(
        last_push_finished_at=d.get("last_push_finished_at"),
        last_push_ok=bool(d.get("last_push_ok", False)),
        active_seconds_at_last_push=int(d.get("active_seconds_at_last_push", 0)),
        prev_connected=d.get("prev_connected"),
    )


def save_state(state: SyncState, path: Path | None = None) -> None:
    p = Path(path) if path is not None else STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(state), indent=2))


# ── active session time ──────────────────────────────────────────────────
def _parse_iso(s: object) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def active_session_seconds(conn: sqlite3.Connection) -> int:
    """Σ over sessions of (max(finished_at) − min(started_at)), in whole seconds.

    A session that hasn't finished a turn falls back to started_at for its end,
    contributing 0 rather than crashing. NULL session_ids are excluded."""
    rows = conn.execute(
        "SELECT session_id, MIN(started_at), MAX(COALESCE(finished_at, started_at)) "
        "FROM turns WHERE session_id IS NOT NULL GROUP BY session_id"
    ).fetchall()
    total = 0
    for r in rows:
        a, b = _parse_iso(r[1]), _parse_iso(r[2])
        if a and b:
            secs = (b - a).total_seconds()
            if secs > 0:
                total += int(secs)
    return total


# ── connectivity ─────────────────────────────────────────────────────────
def probe_connected(host: str | None = None, opts: list[str] | None = None) -> bool:
    """`ssh <opts> <host> true` → exit 0 means reachable. Any error → False."""
    host = host if host is not None else ssh_host()
    opts = opts if opts is not None else _ssh_opts()
    try:
        cp = _run(["ssh", *opts, host, "true"], timeout=15)
        return cp.returncode == 0
    except Exception:
        return False


# ── snapshot + push ──────────────────────────────────────────────────────
def make_snapshot(db_path: Path | None = None, dest: Path | None = None) -> Path:
    """Consistent copy of the live DB via SQLite's online-backup API, so we
    never rsync a file mid-write. Returns the snapshot path."""
    src_path = Path(db_path) if db_path is not None else DB_PATH
    out = Path(dest) if dest is not None else SNAPSHOT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return out


def ensure_remote_dir() -> None:
    cp = _run(["ssh", *_ssh_opts(), ssh_host(), f"mkdir -p {store_dir()}"], timeout=15)
    if cp.returncode != 0:
        raise RoccoSyncError(f"remote mkdir failed ({cp.returncode}): {cp.stderr.strip()}")


def rsync_push(snapshot: Path) -> None:
    transport = "ssh " + " ".join(_ssh_opts())
    cp = _run(["rsync", "-a", "--partial", "--inplace", "-e", transport,
               str(snapshot), _remote_target()], timeout=3600)
    if cp.returncode != 0:
        raise RoccoSyncError(
            f"rsync push failed ({cp.returncode}): {cp.stderr.strip()[-300:]}")


def push() -> dict:
    """Snapshot the DB, rsync it to Rocco, ship a manifest. Raises on failure."""
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = int(src.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
    finally:
        src.close()

    ensure_remote_dir()
    snap = make_snapshot()
    rsync_push(snap)

    manifest = {"rows": rows, "bytes": snap.stat().st_size, "finished_at": _now_iso()}
    mpath = snap.parent / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    transport = "ssh " + " ".join(_ssh_opts())
    _run(["rsync", "-a", "-e", transport, str(mpath),
          f"{ssh_host()}:{store_dir()}/manifest.json"], timeout=60)
    return manifest


# ── decision ─────────────────────────────────────────────────────────────
def decide(state: SyncState, connected: bool, active_now: int) -> tuple[bool, str]:
    if not connected:
        return (False, "offline")
    if state.last_push_finished_at is None:
        return (True, "bootstrap")
    if not state.last_push_ok:
        return (True, "retry")
    if state.prev_connected is False:
        return (True, "reconnect")
    if active_now - state.active_seconds_at_last_push >= THRESHOLD_S:
        return (True, "active-2h")
    return (False, "hold")


# ── tick (driven by the launchd agent) ───────────────────────────────────
def tick(*, db_path: Path | None = None) -> dict:
    state = load_state()
    connected = probe_connected()

    dbp = Path(db_path) if db_path is not None else DB_PATH
    try:
        conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        try:
            active_now = active_session_seconds(conn)
        finally:
            conn.close()
    except Exception:
        active_now = state.active_seconds_at_last_push

    should, reason = decide(state, connected, active_now)
    result: dict = {"connected": connected, "active_seconds": active_now,
                    "decision": reason, "pushed": False}

    if should:
        try:
            manifest = push()
            state.last_push_finished_at = manifest["finished_at"]
            state.last_push_ok = True
            state.active_seconds_at_last_push = active_now
            result["pushed"] = True
            result["manifest"] = manifest
        except Exception as e:
            state.last_push_finished_at = _now_iso()
            state.last_push_ok = False
            result["error"] = str(e)

    state.prev_connected = connected
    save_state(state)
    return result


# ── restore (disaster recovery on startup) ───────────────────────────────
def restore(*, force: bool = False, db_path: Path | None = None) -> dict:
    dbp = Path(db_path) if db_path is not None else DB_PATH
    present = dbp.exists() and dbp.stat().st_size > 0
    if present and not force:
        return {"restored": False, "reason": "local DB present"}

    dbp.parent.mkdir(parents=True, exist_ok=True)
    # Download to a temp path and atomically rename on success, so the final DB
    # path NEVER appears as a truncated/partial file. A truncated prompts.db
    # would look "present" and permanently block a re-restore while serving a
    # corrupt corpus.
    tmp = dbp.with_name(dbp.name + ".restore-tmp")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    transport = "ssh " + " ".join(_ssh_opts())
    cp = _run(["rsync", "-a", "--inplace", "-e", transport,
               _remote_target(), str(tmp)], timeout=3600)
    if cp.returncode != 0 or not tmp.exists():
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return {"restored": False,
                "reason": f"rsync pull failed ({cp.returncode}): {cp.stderr.strip()[-200:]}"}

    os.replace(tmp, dbp)  # atomic publish
    return {"restored": True,
            "bytes": dbp.stat().st_size if dbp.exists() else 0}


# ── CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="telemetrify-rocco-sync",
        description="Replicate the local corpus to Rocco persistent storage.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("tick", help="Evaluate triggers and push if due.")
    rp = sub.add_parser("restore", help="Pull the corpus back from Rocco (if local is missing).")
    rp.add_argument("--force", action="store_true", help="Overwrite a present local DB.")
    sub.add_parser("status", help="Print current sync state.")

    args = p.parse_args(argv)
    if args.cmd == "tick":
        print(json.dumps(tick()))
        return 0
    if args.cmd == "restore":
        print(json.dumps(restore(force=args.force)))
        return 0
    if args.cmd == "status":
        st = load_state()
        print(json.dumps({**asdict(st), "host": ssh_host(),
                          "store": store_dir(), "threshold_s": THRESHOLD_S}))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
