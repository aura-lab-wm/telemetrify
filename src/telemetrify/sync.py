"""Phase 8 sync CLI — stub.

Today this does the safe half of a sqlite -> Postgres replication:
- Instantiate `PostgresBackend(dsn)`.
- Run `apply_schema()` which prints a translated DDL preview.
- Print a "would copy N rows" message.

No rows are read; no network is hit. The actual copy lands in a follow-up
once `PostgresBackend` grows real `connect()` / `insert_turn()` impls.
"""
from __future__ import annotations

import argparse
import sys

from .backends.postgres import PostgresBackend, _mask_dsn


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telemetrify-sync",
        description="Sync local SQLite telemetry to a remote Postgres (stub).",
    )
    p.add_argument(
        "--target",
        required=True,
        help="Postgres DSN, e.g. postgresql://user:pass@host/db",
    )
    # Tri-state via mutually exclusive flags; default True for safety in this phase.
    dry = p.add_mutually_exclusive_group()
    dry.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Preview only (default).",
    )
    dry.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="(Reserved.) Execute the sync. Not yet implemented.",
    )
    p.set_defaults(dry_run=True)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit rows scanned from SQLite (for testing).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    backend = PostgresBackend(args.target)
    backend.apply_schema()

    # Stubbed row count -- we don't actually open SQLite in this phase to keep
    # the smoke test hermetic (no DB file required to verify the preview path).
    n = args.limit if args.limit is not None else 0
    mode = "dry-run" if args.dry_run else "execute"
    print()
    print(
        f"# would copy {n} rows from sqlite -> postgres "
        f"(target={_mask_dsn(args.target)}, mode={mode})"
    )
    if not args.dry_run:
        print("# NOTE: --no-dry-run is reserved; the executor is not wired up yet.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
