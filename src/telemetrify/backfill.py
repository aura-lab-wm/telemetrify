"""Walk every Claude Code transcript on disk and ingest every turn we haven't already
captured. Idempotent: re-runs are safe because of the user_uuid UNIQUE constraint.

Usage:
    python -m telemetrify.backfill                    # all transcripts under ~/.claude/projects
    python -m telemetrify.backfill /path/to/file.jsonl
    python -m telemetrify.backfill --no-embed         # skip embedding (fast bulk load)
    python -m telemetrify.backfill --since 2026-04-01 # only files modified since
"""
import argparse
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .db import connect
from .store import upsert_session, insert_turn, record_ingest_run
from .transcript import iter_all_turns


def find_transcripts(root: Path, since: datetime | None) -> list[Path]:
    files = list(root.glob("*/*.jsonl"))
    if since is not None:
        cutoff = since.timestamp()
        files = [f for f in files if f.stat().st_mtime >= cutoff]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def ingest_file(conn, path: Path, do_embed: bool) -> tuple[int, int, int]:
    inserted = skipped = errors = 0
    embed_fn = None
    if do_embed:
        from .embed import embed_turn as _e
        embed_fn = _e

    for turn in iter_all_turns(path):
        try:
            with conn:
                upsert_session(conn, turn)
                vec = embed_fn(turn.user_text, turn.assistant_text) if embed_fn else None
                turn_id = insert_turn(conn, turn, vec)
                if turn_id is None:
                    skipped += 1
                else:
                    inserted += 1
                    # run_events are populated inside insert_turn (commit=False,
                    # joining this `with conn:` transaction) — no separate
                    # derive call here, which would nest a `with conn:` and
                    # flush the partial turn.
        except Exception:
            errors += 1
            print(f"[error] {path.name}: {traceback.format_exc()}", file=sys.stderr)
    return inserted, skipped, errors


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="*", help="transcript files or dirs; default ~/.claude/projects")
    p.add_argument("--no-embed", action="store_true", help="skip embedding (bulk-load mode)")
    p.add_argument("--since", help="ISO date, only files modified on/after this")
    args = p.parse_args(argv)

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    targets: list[Path] = []
    if args.paths:
        for raw in args.paths:
            pth = Path(raw).expanduser()
            if pth.is_dir():
                targets.extend(find_transcripts(pth, since))
            elif pth.is_file():
                targets.append(pth)
    else:
        targets = find_transcripts(Path.home() / ".claude" / "projects", since)

    print(f"found {len(targets)} transcript files")
    conn = connect()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tot_i = tot_s = tot_e = 0
    for i, path in enumerate(targets, 1):
        ins, sk, er = ingest_file(conn, path, do_embed=not args.no_embed)
        tot_i += ins; tot_s += sk; tot_e += er
        print(f"[{i}/{len(targets)}] {path.name} → +{ins} ={sk} !{er}", flush=True)

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        record_ingest_run(conn, "backfill", started_at, finished_at,
                          tot_i, tot_s, tot_e, f"{len(targets)} files")
    print(f"done: inserted={tot_i} skipped={tot_s} errors={tot_e}")
    return 0 if tot_e == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
