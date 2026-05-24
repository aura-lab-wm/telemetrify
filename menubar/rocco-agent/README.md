# rocco-agent

Tiny stdlib-only Python daemon that runs on **Rocco** (the W&M CS lab GPU box)
and writes a JSON snapshot of GPU / vLLM / listening-port state to
`~/.cache/rocco-status.json` every 5 seconds. The Mac menubar app
(`rocco-pulse`) reads that file over SSH (`ssh rocco cat
~/.cache/rocco-status.json`) to render its popover.

The agent itself never runs on the Mac. Tests run locally via `pytest`.

## Install (one-liner)

From the telemetrify repo root, on your Mac:

```bash
bash menubar/rocco-agent/install.sh rocco
```

This will:

1. `scp rocco-agent.py` to `~/.local/bin/` on the remote host
2. `scp rocco-agent.service` to `~/.config/systemd/user/`
3. `sudo loginctl enable-linger <you>` so the user manager survives logout
4. `systemctl --user daemon-reload && enable --now rocco-agent`

Re-running the script just refreshes the files and restarts the service.

You can pass a different hostname:

```bash
bash menubar/rocco-agent/install.sh rocco.cs.wm.edu
```

The host alias must be reachable over SSH with key auth (no password
prompts). The recommended `~/.ssh/config` block is in `menubar/README.md`.

## Verify

```bash
ssh rocco systemctl --user status rocco-agent
ssh rocco cat ~/.cache/rocco-status.json | jq .
ssh rocco journalctl --user -u rocco-agent -f       # tail logs
```

## Output schema (v1)

```jsonc
{
  "schema_version": 1,
  "host": "rocco.cs.wm.edu",
  "ts": 1737759600,
  "agent_uptime_s": 12345,
  "gpus": [
    {"idx": 0, "name": "NVIDIA A100-SXM4-80GB",
     "util_pct": 42, "mem_used_mib": 36210, "mem_total_mib": 81920,
     "temp_c": 63, "power_w": 210.1}
  ],
  "vllm": {"running": true, "model": "Kimi-Dev-72B",
           "port": 8000, "pid": 1234, "uptime_s": null},
  "services": [{"port": 8000, "proc": "vllm", "pid": 1234},
               {"port": 22,   "proc": "sshd", "pid": 987}],
  "tier": 5,
  "tier_reason": "vLLM up (Kimi-Dev-72B)",
  "inference_recent": null,
  "errors": []
}
```

The authoritative schema lives in the plan at
`/Users/amastro/.claude/plans/sunny-beaming-church.md` ("Status JSON schema"
section under Track B).

### Tier semantics

| Tier | Meaning                                                            |
|------|--------------------------------------------------------------------|
| 5    | vLLM serving AND at least one GPU idle (best — local /ask works)   |
| 4    | >=4 idle GPUs (vLLM down — easy to bring up)                       |
| 3    | 2-3 idle GPUs                                                      |
| 2    | 1 idle GPU                                                         |
| 1    | 0 idle GPUs (worst — fall back to cloud)                           |

"Idle" = `nvidia-smi` reported `utilization.gpu` strictly below 5%.

## Tests

```bash
cd /Users/amastro/Projects/telemetrify
.venv/bin/pytest menubar/rocco-agent/tests/ -v
```

Tests are pure-Python, no SSH or GPUs required. Fixtures live in
`tests/fixtures/`.

## Troubleshooting

| Symptom                                  | Check                                                                     |
|------------------------------------------|---------------------------------------------------------------------------|
| `rocco-status.json` does not exist       | `ssh rocco systemctl --user status rocco-agent` — is the unit active?      |
| Stale `ts` (older than ~30s)             | `ssh rocco journalctl --user -u rocco-agent -n 50` — sample errors?        |
| `gpus` is empty, `errors` mentions nvidia-smi | Driver missing or `nvidia-smi` not on PATH for the user session.     |
| `services` is empty                      | `ss -tlnH` missing — install `iproute2`.                                   |
| `vllm.running=false` when vLLM IS up     | Wrong port? Override with `Environment=ROCCO_VLLM_BASE_URL=http://localhost:PORT` in the unit file. |
| `enable-linger` failed during install    | Re-run install with sudo access, or the agent will stop when you log out. |
| Permission denied on scp                 | Confirm passwordless SSH: `ssh -o BatchMode=yes rocco echo ok`.            |

## Uninstall

```bash
ssh rocco '
  systemctl --user disable --now rocco-agent || true
  rm -f ~/.config/systemd/user/rocco-agent.service ~/.local/bin/rocco-agent.py
  systemctl --user daemon-reload
'
```

## Design notes

- **Stdlib only.** Rocco is a shared lab box; we do not get to install pip
  packages globally. `subprocess`, `urllib`, `json`, `socket` cover everything.
- **Never crashes.** Every per-sample exception is caught, recorded in
  `snapshot["errors"]`, logged to stderr; the loop keeps running.
- **Atomic writes.** `open(.tmp) -> fsync -> os.replace(.tmp, target)` so the
  Mac side reading concurrently can never see a half-written file.
- **No root.** systemd `--user` only. The single sudo step is
  `loginctl enable-linger`, which can be skipped (agent then only runs while
  you have a login session).
