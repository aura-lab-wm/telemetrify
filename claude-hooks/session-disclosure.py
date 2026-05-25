#!/usr/bin/env python3
"""SessionStart hook: show a once-per-day recording disclosure.

telemetrify captures Claude Code work into a local SQLite DB. This pops a
non-blocking styled toast (the DistillToast HUD) the first time a session
starts each day, so the user is reminded that the session is recorded and
what gets saved. Atomic dated marker => exactly one toast per calendar day
even across concurrent session starts. Exits 0 on anything odd so it can
never disrupt session start.
"""
import sys, os, json, glob, subprocess, datetime

HOOKS = os.path.expanduser("~/.claude/hooks")
APP = os.path.join(HOOKS, "DistillToast.app")
ICON = os.path.join(HOOKS, "disclosure-icon.png")
G1, G2 = "f5a623", "c1440e"  # amber "notice" gradient (distinct from the magenta distill nudge)


def main() -> None:
    try:
        json.load(sys.stdin)  # consume hook payload (unused)
    except Exception:
        pass

    today = datetime.date.today().isoformat()
    marker = os.path.join(HOOKS, f".disclosure-{today}")

    # atomic once-per-day claim — first session of the day wins, rest skip
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        sys.exit(0)
    except Exception:
        sys.exit(0)

    # tidy older markers
    for old in glob.glob(os.path.join(HOOKS, ".disclosure-*")):
        if not old.endswith(today):
            try:
                os.remove(old)
            except Exception:
                pass

    title = "This session is recorded"
    subtitle = "telemetrify captures your Claude Code work — stored locally"
    body = ("Saved: your prompts & replies, tool calls & output, model, token "
            "counts, timestamps, cwd & git branch.")

    if os.path.isdir(APP):
        cmd = ["open", "-n", "-g", APP, "--args", title, subtitle, body]
        if os.path.exists(ICON):
            cmd += [ICON, G1, G2]
        try:
            subprocess.run(cmd, timeout=5, check=False)
        except Exception:
            pass

    print("recording disclosure shown")
    sys.exit(0)


if __name__ == "__main__":
    main()
