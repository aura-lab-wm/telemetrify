# rocco-pulse — telemetrify's operational dashboard for its remote LLM backend

This is **not a standalone tray app.** It's a subordinate of the parent
[`telemetrify`](../README.md) project, and it exists because telemetrify runs
its heavy LLM inference (`/ask`, planner, synthesizer) on **Rocco** — a shared
CS-lab GPU server that hosts a 72B-parameter model (Kimi-Dev-72B BF16, ~145 GB,
4× L40S). Without rocco-pulse you have no visible signal whether your next
`/ask` call will land on Rocco (free, fast, private) or silently fall through
to your Anthropic OAuth bucket (paid, rate-limited, leaves the box).

**What it surfaces** (macOS menu-bar, no dock icon, no main window):
- 🟢/🔴 tier badge (4 GPUs free → green; all busy → red), live model name
- GPU util / mem / temp per card
- vLLM up/down · port · "what model is configured to load"
- One-click **Start / Stop** for the remote vLLM (recycles `model_manager.py`
  on Rocco; no manual SSH)
- AI-classified "Unknown ports" — calls telemetrify's `/api/classify-ports`
  to label random listeners (ZMQ / IPython kernel / Jupyter / …) using
  the same LLM router stack `/ask` uses

**The bolt indicator** on the menubar icon goes from circle → glowing
lightning-bolt the moment Rocco's vLLM is up — so you can tell at a glance
that the inference rig is hot before you even open the popover.

```
~/Projects/telemetrify/menubar/
├── project.yml                  xcodegen spec (RoccoPulseCore + RoccoPulseApp)
├── Makefile                     generate / test / build / install
├── RoccoPulseCore/Sources/      RoccoStatus · SSHProbe · StatusStore ·
│                                LifecycleCommands · TierPalette · ProcessLauncher
├── RoccoPulseApp/Sources/       RoccoPulseApp (@main) · AppDelegate ·
│                                StatusView · MenuBarIcon
├── RoccoPulseCoreTests/         5 XCTest files + JSON fixtures
└── rocco-agent/                 Python agent that runs on Rocco itself
                                 (managed by the parallel Track-B agent task)
```

## Install order

1. **Install the Rocco-side agent first.** It writes
   `~/.cache/rocco-status.json` every 5s; this app is just a renderer.
   ```sh
   bash menubar/rocco-agent/install.sh rocco
   ssh rocco systemctl --user status rocco-agent   # → active (running)
   ```
2. **Add a `Host rocco` block to `~/.ssh/config` on the Mac.** ControlMaster
   is required for the menubar to feel snappy — without it, every poll pays
   the full SSH handshake (~600ms instead of ~40ms):
   ```
   Host rocco
     HostName rocco.cs.wm.edu
     Port 13110
     User amastropaolo
     ControlMaster auto
     ControlPath ~/.ssh/cm-%r@%h:%p
     ControlPersist 10m
     ServerAliveInterval 30
     ServerAliveCountMax 3
   ```
3. **Build + install the app:**
   ```sh
   brew install xcodegen   # one-time prerequisite
   cd menubar
   make install
   ```
   `make install` runs `xcodegen generate`, builds Release, copies the .app
   to `/Applications/rocco-pulse.app`, and opens it. The menubar icon
   appears within ~2s.

## Common make targets

| Command | What it does |
| --- | --- |
| `make generate` | regenerate `rocco-pulse.xcodeproj` from `project.yml` |
| `make test` | run `RoccoPulseCoreTests` (23 tests, no SSH, no GPU) |
| `make build` | Debug build to `build/Build/Products/Debug/rocco-pulse.app` |
| `make release` | Release build |
| `make install` | Release + copy to `/Applications` + open |
| `make clean` | wipe derived data and the generated xcodeproj |

## Architecture summary

* **`SSHProbe`** — shells out to `ssh rocco cat ~/.cache/rocco-status.json`
  with `-o BatchMode=yes -o ConnectTimeout=4 -o ServerAliveInterval=30`.
  Runs on a background `DispatchQueue` with a 6s hard timeout. A
  `ProcessLauncher` protocol is the only injection point so XCTests never
  fork a real `ssh`.
* **`StatusStore`** — `@MainActor` `ObservableObject` driven by a `Timer`
  (default 15s, toggleable to 5s / 60s from the popover footer). Persists
  the last good snapshot to `Application Support/rocco-pulse/last.json` so
  the UI still shows "last seen 12 min ago" after a relaunch.
* **`MenuBarIcon`** — derives both the SF Symbol and the `.foregroundStyle`
  color from `(snapshot, lastError, freshness)` via `IconState` +
  `TierPalette`. v0 uses `waveform.path.ecg`; the icon contract is
  independent of the symbol so a custom asset can drop in later.
* **`LifecycleCommands`** — `startVLLM()` / `stopVLLM()` shell out to
  `ssh rocco "cd /scratch/amastropaolo/rocco-inference && .venv/bin/python -m model_manager.manager up|down"`
  and forward stdout line-by-line to a delegate so the popover can show
  progress. The remote `manager.py` daemonizes via a double-fork on `up`, so
  the SSH call returns immediately rather than blocking on the poll loop.
* **No `WindowGroup` in the App Scene** — only `MenuBarExtra`. This avoids
  the LSUIElement-vs-WindowGroup activation race aura-pulse hit, and keeps
  the dock icon out.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Red icon, "Permission denied (publickey)" | SSH key is locked | `ssh-add ~/.ssh/id_ed25519` |
| Icon never turns green | agent not running | `ssh rocco systemctl --user status rocco-agent` |
| Yellow "stale" badge | agent died or laptop is asleep | `ssh rocco systemctl --user restart rocco-agent` |
| Icon disappears after upgrade | macOS LaunchServices cache | `killall Dock`; reopen the .app |

## What's NOT done yet

* No custom app icon — the .app bundle uses the Xcode default. SF Symbol
  inside the menubar is final.
* No Settings UI — `pollInterval` and persistence path are toggleable
  in-code only.
* No "Launch at login" registration — register manually via
  System Settings → Login Items → "+" → `/Applications/rocco-pulse.app`.
