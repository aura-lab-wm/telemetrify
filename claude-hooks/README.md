# claude-hooks — Claude Code session hooks for telemetrify

Claude Code [hooks](https://docs.claude.com/en/docs/claude-code/hooks) that run
around your coding sessions and tie into the telemetrify pipeline. They live here
(not loose in `~/.claude/`) so they're versioned with the project they serve, and
are **symlinked** into `~/.claude/hooks/` so Claude Code can find them.

telemetrify already ships one Claude Code hook — `bin/capture-hook` (the `Stop`
hook that records each turn into `data/prompts.db`). These two add the session
*bookends*:

| Script | Event | Fires when | What it does |
|---|---|---|---|
| `../bin/capture-hook` | `Stop` | every turn | writes the completed turn to the local SQLite DB |
| `session-disclosure.py` | `SessionStart` | first session of each day | shows a "this session is recorded" disclosure |
| `distill-reminder.py` | `SessionEnd` | session ran >30 min **or** >5 turns | nudges you to run `/distill` before context fades |

All three are non-blocking and exit `0` on any odd input — a hook must never
disrupt session start/exit.

## The toast HUD

Both new hooks render through **`DistillToast.app`** — a tiny self-owned floating
panel (`distill-toast.swift`), not a macOS notification. Why: native
notifications can't be themed (no gradients/custom layout) and depend on
per-bundle banner permissions that may be off. The HUD always renders, is fully
styled, and is launched via `open` so LaunchServices owns it — it survives Claude
Code's own exit (critical for the `SessionEnd` case).

```
distill-toast  <title> <subtitle> <body> [iconPath] [hex1] [hex2]
```

- Auto-sizes its height to the body (1–3 lines).
- **Stacks** concurrent toasts (multiple sessions ending at once) via
  PID-stamped slot locks in `/tmp/distill-toast-slot-*.lock` — up to 6, stale
  locks reclaimed, released on fade-out.
- Default theme is magenta (`distill-icon.png`, a sealed-checkmark glyph); the
  disclosure passes the amber `disclosure-icon.png` + its own colors.

## Files

```
distill-reminder.py     SessionEnd  → /distill nudge
session-disclosure.py   SessionStart→ once-per-day recording disclosure
distill-toast.swift     the styled HUD (source)
DistillToast.app/       app bundle launched via `open` (binary is built, gitignored)
distill-icon.png        magenta sealed-checkmark icon (distill nudge)
disclosure-icon.png     amber record-dot icon (disclosure)
build.sh                recompiles the Swift HUD into the bundle + loose binary
```

Compiled binaries (`distill-toast`, `DistillToast.app/Contents/MacOS/DistillToast`)
are **gitignored** — reproduce them with `./build.sh`.

## Install

1. **Build the HUD:**
   ```sh
   ./build.sh
   ```

2. **Symlink into `~/.claude/hooks/`** (so Claude Code's absolute hook paths and
   the HUD's `~/.claude/hooks/*.png` references resolve):
   ```sh
   for f in distill-reminder.py session-disclosure.py distill-toast.swift \
            distill-icon.png disclosure-icon.png distill-toast DistillToast.app; do
     ln -sf "$PWD/$f" ~/.claude/hooks/"$f"
   done
   ```

3. **Register the hooks** in `~/.claude/settings.json` (merge into any existing
   `hooks` block — do **not** replace it). This file is *not* in the repo because
   it holds personal secrets; the snippet to add:
   ```json
   "SessionStart": [
     { "hooks": [ { "type": "command",
       "command": "python3 /Users/<you>/.claude/hooks/session-disclosure.py",
       "timeout": 10 } ] }
   ],
   "SessionEnd": [
     { "hooks": [ { "type": "command",
       "command": "python3 /Users/<you>/.claude/hooks/distill-reminder.py",
       "timeout": 10 } ] }
   ]
   ```
   Open `/hooks` in Claude Code once (or restart) so the new config is picked up.

## Develop

Edit the real files here, then `./build.sh` if you touched `distill-toast.swift`.
Test a hook without ending a session:

```sh
# disclosure (clear the daily marker first so it isn't suppressed)
rm -f ~/.claude/hooks/.disclosure-*
echo '{}' | python3 session-disclosure.py

# distill nudge (point at any real transcript jsonl)
echo '{"transcript_path":"/path/to/session.jsonl"}' | python3 distill-reminder.py
```

The disclosure copy is grounded in telemetrify's capture schema (`sessions` +
`turns` + `tool_calls` in `data/prompts.db`): it lists exactly what's stored —
prompts & replies, tool calls & output, model, token counts, timestamps, cwd &
git branch. Keep it accurate if the schema changes.
