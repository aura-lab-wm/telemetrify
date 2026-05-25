#!/usr/bin/env python3
"""SessionEnd hook: nudge to run /distill if the session is non-trivial.

Fires when the session ran > 30 min OR had > 5 iterations (user turns).
Reads the hook JSON on stdin (has `transcript_path`), derives both signals from
the JSONL transcript (first timestamp vs now; count of user-role entries), and —
if either threshold is crossed — pops a macOS notification + prints a line.
Exits 0 on ANY missing/odd field so it can never disrupt session exit.

(A SessionEnd hook can't pause for a Y/N — the session is closing — so this is a
question-framed nudge; the actual interactive ask happens in-session, see the
[[feedback-distill-reminder]] behavior memory.)
"""
import sys, os, json, shutil, subprocess, datetime

THRESHOLD_SECONDS = 1800  # 30 minutes
THRESHOLD_TURNS = 5       # > 5 iterations (user turns)
ICON = os.path.expanduser("~/.claude/hooks/distill-icon.png")
APP = os.path.expanduser("~/.claude/hooks/DistillToast.app")
TOAST = os.path.expanduser("~/.claude/hooks/distill-toast")


def _notify(title: str, subtitle: str, body: str) -> None:
    """Deliver the nudge with the best available surface, most-robust first:
      1. DistillToast.app via `open` — a self-owned styled gradient HUD launched
         through LaunchServices, so it's fully decoupled from this hook and
         survives Claude Code's own exit. Fully themed; no notification
         permission needed. (-n new instance, -g don't steal focus.)
      2. distill-toast loose binary, detached — fallback if the bundle is gone.
      3. terminal-notifier — native banner with the custom icon (needs the user
         to have granted it banner permission).
      4. osascript — plain native banner (always allowed, but un-themed).
    """
    if os.path.isdir(APP):
        try:
            subprocess.run(
                ["open", "-n", "-g", APP, "--args", title, subtitle, body],
                timeout=5, check=False,
            )
            return
        except Exception:
            pass

    if os.path.exists(TOAST) and os.access(TOAST, os.X_OK):
        try:
            subprocess.Popen(
                [TOAST, title, subtitle, body],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass

    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [tn, "-title", title, "-subtitle", subtitle, "-message", body,
               "-sound", "Glass", "-group", "distill-reminder"]
        if os.path.exists(ICON):
            cmd += ["-appIcon", ICON, "-contentImage", ICON]
        subprocess.run(cmd, timeout=5, check=False)
        return

    subprocess.run(
        ["osascript", "-e",
         f'display notification "{body}" with title "{title}" '
         f'subtitle "{subtitle}" sound name "Glass"'],
        timeout=5, check=False,
    )


def _bail():
    sys.exit(0)


def _is_user_turn(obj: dict) -> bool:
    if obj.get("type") == "user":
        return True
    msg = obj.get("message")
    return isinstance(msg, dict) and msg.get("role") == "user"


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        _bail()

    tp = data.get("transcript_path")
    if not tp or not os.path.exists(tp):
        _bail()

    start = None
    user_turns = 0
    try:
        with open(tp, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if start is None and obj.get("timestamp"):
                    start = obj["timestamp"]
                if _is_user_turn(obj):
                    user_turns += 1
    except Exception:
        _bail()

    dur = 0.0
    if start:
        try:
            t0 = datetime.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            now = datetime.datetime.now(t0.tzinfo) if t0.tzinfo else datetime.datetime.now()
            dur = (now - t0).total_seconds()
        except Exception:
            dur = 0.0

    long_enough = dur > THRESHOLD_SECONDS
    busy_enough = user_turns > THRESHOLD_TURNS
    if not (long_enough or busy_enough):
        _bail()

    # Build a reason string from whichever signal(s) tripped.
    bits = []
    if long_enough:
        bits.append(f"{dur/3600.0:.1f}h" if dur >= 3600 else f"{int(dur/60)}m")
    if busy_enough:
        bits.append(f"{user_turns} turns")
    why = " · ".join(bits)
    try:
        _notify(
            title="distill?",
            subtitle=f"Substantial session · {why}",
            body="Run /distill to bank items before they fade.",
        )
    except Exception:
        pass
    print(f"Session was substantial ({why}) — run /distill to bank items before they fade?")
    sys.exit(0)


if __name__ == "__main__":
    main()
