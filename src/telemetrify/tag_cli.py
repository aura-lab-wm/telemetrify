"""`telemetrify tag` — outcome-stamping CLI (#3).

  telemetrify tag --turn <id> --outcome <tag> [--entity <run_event_id|tool_use_id>]
  telemetrify tag --turn <id> --auto        # re-stamp from output regex
  telemetrify tag --init-config             # write data/outcome_rules.json defaults

Manual stamping sets source='manual'; --auto re-runs the config-driven regexes
(source='auto-output-match') against the turn's captured tool RESULT output.
"""
from __future__ import annotations

import argparse
import json
import sys

from .db import connect
from .run_events import manual_tag, stamp_outcomes_for_turn
from . import outcome_rules


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="telemetrify tag")
    p.add_argument("--turn", type=int, help="turn id to stamp")
    p.add_argument("--outcome", help="outcome_tag to set (manual)")
    p.add_argument("--entity", help="run_event id or tool_use_id to target "
                                    "(default: all the turn's run_events)")
    p.add_argument("--auto", action="store_true",
                   help="re-stamp from the config-driven output regexes")
    p.add_argument("--init-config", action="store_true",
                   help="write data/outcome_rules.json with built-in defaults")
    args = p.parse_args(argv)

    if args.init_config:
        wrote = outcome_rules.write_default_config(force=False)
        print(f"{'wrote' if wrote else 'kept existing'} {outcome_rules.CONFIG_PATH}")
        return 0

    if not args.turn:
        p.error("--turn is required unless --init-config")

    conn = connect()
    if args.auto:
        n = stamp_outcomes_for_turn(conn, args.turn, source="auto-output-match")
        print(json.dumps({"turn_id": args.turn, "tagged": n}, indent=2))
        return 0

    if not args.outcome:
        p.error("--outcome is required for manual stamping (or use --auto)")

    n = manual_tag(conn, args.turn, args.outcome.strip(), entity=args.entity)
    print(json.dumps({
        "turn_id": args.turn,
        "outcome": args.outcome.strip(),
        "entity": args.entity,
        "stamped": n,
    }, indent=2))
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())