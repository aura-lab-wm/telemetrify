"""`telemetrify run-stats` — command success-vs-fail + outcome_tag counts.

CLI:
  bin/run-stats                       # current rates + tag breakdown
  bin/run-stats --trend               # per-week success-vs-fail + unsupported-claim rate
  bin/run-stats --trend --bucket day   # per-day
  bin/run-stats --json                # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys

from .db import connect
from .run_events import command_success_rate, outcome_trend
from .evidence import unsupported_claim_rate, unsupported_trend


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="telemetrify run-stats")
    p.add_argument("--trend", action="store_true", help="per-bucket trend")
    p.add_argument("--bucket", choices=["day", "week"], default="week")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    conn = connect()
    out: dict = {}

    cmd = command_success_rate(conn)
    out["command_outcome"] = {
        "total": cmd["total"],
        "resolved": cmd["resolved"],
        "unresolved": cmd["unresolved"],
        "resolution_rate": cmd["resolution_rate"],
        "success": cmd["success"],
        "failure": cmd["failure"],
        "success_rate_conditional_on_resolution": cmd["rate"],
        "overall_bounds": cmd["bounds"],
        "tag_counts": cmd["tag_counts"],
    }

    ev = unsupported_claim_rate(conn)
    out["evidence"] = {
        "unsupported": ev["unsupported"],
        "backed": ev["backed"],
        "no_claim": ev["no_claim"],
        "unsupported_claim_rate": ev["rate"],
    }

    if args.trend:
        out["command_trend"] = outcome_trend(conn, bucket=args.bucket)
        out["unsupported_trend"] = unsupported_trend(conn, bucket=args.bucket)

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    # Human-readable
    sr = cmd["rate"]
    rr = cmd["resolution_rate"]
    bw = cmd["bounds"]["worst"]
    bb = cmd["bounds"]["best"]
    ur = ev["rate"]
    print("== command outcome ==")
    print(f"  total run_events={cmd['total']}  resolved={cmd['resolved']}  "
          f"unresolved={cmd['unresolved']}  "
          f"resolution_rate={(f'{rr:.1%}' if rr is not None else '—')}")
    print(f"  success={cmd['success']}  failure={cmd['failure']}  "
          f"success_rate|resolved={(f'{sr:.1%}' if sr is not None else '—')}  "
          f"(NOT overall success — see bounds)")
    print(f"  overall success bounds:  worst={(f'{bw:.1%}' if bw is not None else '—')}  "
          f"best={(f'{bb:.1%}' if bb is not None else '—')}  "
          f"[if all {cmd['unresolved']} unresolved are failures / successes]")
    print("  outcome_tag counts:")
    for tag, n in sorted(cmd["tag_counts"].items(), key=lambda kv: -kv[1]):
        print(f"    {tag:20s} {n}")
    print("== evidence backing ==")
    print(f"  unsupported={ev['unsupported']}  backed={ev['backed']}  "
          f"no_claim={ev['no_claim']}  "
          f"unsupported_claim_rate={(f'{ur:.1%}' if ur is not None else '—')}")
    if args.trend:
        print(f"\n== command trend ({args.bucket}) ==")
        for b in out["command_trend"]:
            res = b["success"] + b["failure"]
            r = (b["success"] / res) if res else 0.0
            print(f"  {b['bucket']}  success={b['success']:4d}  failure={b['failure']:4d}  "
                  f"unresolved={b['unresolved']:5d}  rate|resolved={r:.1%}")
        print(f"\n== unsupported-claim trend ({args.bucket}) ==")
        for b in out["unsupported_trend"]:
            print(f"  {b['bucket']}  unsupported={b['unsupported']:3d}  backed={b['backed']:3d}  "
                  f"rate={b['rate']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())