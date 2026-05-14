"""CLI for the push notify channel — `bin/notify` thin wrapper.

    notify --title "..." --body "..." [--url ...] [--dry-run]

Prints a JSON summary of `{sent, failed, removed}` to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys

from .db import connect
from .push_notify import notify


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="notify",
        description="Send a Web Push notification to every active subscription.",
    )
    p.add_argument("--title", required=True, help="notification title")
    p.add_argument("--body", required=True, help="notification body")
    p.add_argument("--url", default=None, help="URL to open on notification click")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would be sent but make no network calls",
    )
    args = p.parse_args(argv)

    conn = connect()
    result = notify(conn, args.title, args.body, url=args.url, dry_run=args.dry_run)
    print(json.dumps(result))
    # Non-zero only when there was at least one real failure.
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
