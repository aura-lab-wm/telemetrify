"""bin/insights → python -m telemetrify.insights_cli.

A thin CLI shell around compute_all() — keeps the module testable
without bin/-bash plumbing showing up in test imports.
"""
from __future__ import annotations

import argparse
import sys

from .db import connect
from .insights import compute_all, DEFAULT_FRESHNESS_S


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insights",
        description="Pre-compute curated /insights answers.")
    p.add_argument("--force", action="store_true",
                   help="Re-run every question even if cached entry is fresh.")
    p.add_argument("--ttl-hours", type=float,
                   default=DEFAULT_FRESHNESS_S / 3600,
                   help="Skip entries fresher than this (default 23h).")
    args = p.parse_args(argv)

    conn = connect()
    payload = compute_all(
        conn,
        force_refresh=args.force,
        ttl_s=int(args.ttl_hours * 3600),
    )
    print(f"[insights] {len(payload['entries'])} entries cached.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
