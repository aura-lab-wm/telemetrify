#!/usr/bin/env python3
"""Rocco status agent.

Runs as a systemd --user service on Rocco (Ubuntu CS lab box). Every poll
interval, it samples GPU state (nvidia-smi), listening sockets (ss -tlnH),
and the local vLLM server (HTTP GET /v1/models), then writes a single JSON
snapshot atomically to ``~/.cache/rocco-status.json``.

The Mac menubar app (rocco-pulse) reads that file over SSH:
    ssh rocco cat ~/.cache/rocco-status.json

Design constraints:
- Python 3.10+, **stdlib only** (this runs on a bare lab box; no pip installs).
- Never crashes the daemon — every per-sample failure is logged to stderr and
  recorded in ``snapshot["errors"]``; the loop continues.
- Atomic writes (``open(.tmp) -> fsync -> os.replace``) so the reader can
  never observe a half-written file.
- No root required. systemd --user only.

Schema: see ``menubar/rocco-agent/README.md`` (schema_version=1).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants / config
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
POLL_INTERVAL_S = 5.0
VLLM_BASE_URL = os.environ.get("ROCCO_VLLM_BASE_URL", "http://localhost:8000")
VLLM_PROBE_TIMEOUT_S = 1.0
STATUS_PATH = Path(
    os.environ.get(
        "ROCCO_STATUS_PATH",
        str(Path.home() / ".cache" / "rocco-status.json"),
    )
)
IDLE_UTIL_THRESHOLD_PCT = 5  # "idle" = GPU util strictly below this

# Process start time, used to compute agent_uptime_s.
_AGENT_START_TS = time.time()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_nvidia_smi(csv_text: str) -> list[dict[str, Any]]:
    """Parse the CSV emitted by:

        nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,
                   memory.total,temperature.gpu,power.draw
                   --format=csv,noheader,nounits

    Returns a list of dicts, one per GPU, in index order.
    """
    out: list[dict[str, Any]] = []
    for line in csv_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            out.append(
                {
                    "idx": int(parts[0]),
                    "name": parts[1],
                    "util_pct": int(float(parts[2])),
                    "mem_used_mib": int(float(parts[3])),
                    "mem_total_mib": int(float(parts[4])),
                    "temp_c": int(float(parts[5])),
                    "power_w": float(parts[6]),
                }
            )
        except (ValueError, IndexError):
            # Skip malformed row but keep going.
            continue
    return out


def parse_ss_tlnH(text: str) -> list[dict[str, Any]]:
    """Parse the output of ``ss -tlnH``.

    Each row roughly looks like:
        LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:* users:(("vllm",pid=1234,fd=7))

    Returns a deduplicated list of dicts: {port, proc, pid}.
    Dedup key is (port, proc, pid) — IPv4 and IPv6 listeners on the same
    port collapse to one entry.
    """
    services: dict[tuple[int, str, int | None], dict[str, Any]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        cols = line.split()
        if len(cols) < 4:
            continue
        # Local-Address:Port is typically col index 3 (LISTEN 0 4096 addr:port ...)
        local = cols[3]
        # Strip brackets for IPv6, then take the trailing :port.
        local_clean = local.replace("[", "").replace("]", "")
        if ":" not in local_clean:
            continue
        port_str = local_clean.rsplit(":", 1)[1]
        try:
            port = int(port_str)
        except ValueError:
            continue

        proc = ""
        pid: int | None = None
        # Find users:((...)) blob — may not be present without -p / permissions.
        if "users:" in line:
            blob = line[line.index("users:") :]
            # crude extract: first quoted name, first pid=N
            try:
                first_quote = blob.index('"')
                second_quote = blob.index('"', first_quote + 1)
                proc = blob[first_quote + 1 : second_quote]
            except ValueError:
                proc = ""
            if "pid=" in blob:
                pid_tail = blob.split("pid=", 1)[1]
                num = []
                for ch in pid_tail:
                    if ch.isdigit():
                        num.append(ch)
                    else:
                        break
                if num:
                    try:
                        pid = int("".join(num))
                    except ValueError:
                        pid = None

        key = (port, proc, pid)
        services.setdefault(
            key,
            {"port": port, "proc": proc, "pid": pid},
        )
    return list(services.values())


# ---------------------------------------------------------------------------
# vLLM probe
# ---------------------------------------------------------------------------


def probe_vllm(base_url: str, timeout: float = VLLM_PROBE_TIMEOUT_S) -> dict[str, Any]:
    """GET {base_url}/v1/models with a short timeout.

    200 → parse data[0].id as the loaded model.
    Anything else (URLError, HTTPError, timeout, non-200) → running=False.
    """
    url = base_url.rstrip("/") + "/v1/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                return {"running": False, "model": None}
            body = resp.read()
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {"running": True, "model": None}
            data = payload.get("data") or []
            model_name = None
            if data and isinstance(data, list):
                first = data[0]
                if isinstance(first, dict):
                    full_id = first.get("id") or ""
                    # Strip any "org/" prefix for nicer display.
                    model_name = full_id.rsplit("/", 1)[-1] if full_id else None
            return {"running": True, "model": model_name}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return {"running": False, "model": None}
    except Exception:  # noqa: BLE001 — never crash the daemon
        return {"running": False, "model": None}


# ---------------------------------------------------------------------------
# Tier logic
# ---------------------------------------------------------------------------


def compute_tier(
    gpus: list[dict[str, Any]], vllm: dict[str, Any]
) -> tuple[int, str]:
    """Map (gpus, vllm) to a 1..5 readiness tier + a human reason string.

    Idle-count is the primary driver — a box with vLLM running but every GPU
    pinned by some other job is still not useful for new requests. vLLM-up
    only bumps the tier to 5 when there is at least one free GPU to take a
    new request.

    5 = vLLM up AND at least one GPU idle (best — local /ask can be served)
    4 = >=4 idle GPUs (vLLM down — could be brought up easily)
    3 = 2-3 idle GPUs
    2 = 1 idle GPU
    1 = no idle GPUs (worst — fall back to cloud)
    """
    idle = sum(1 for g in gpus if int(g.get("util_pct", 0)) < IDLE_UTIL_THRESHOLD_PCT)
    if idle == 0:
        return 1, "no free GPUs"
    if vllm.get("running"):
        model = vllm.get("model") or "model"
        return 5, f"vLLM up ({model})"
    if idle >= 4:
        return 4, f"{idle} GPUs free"
    if idle >= 2:
        return 3, f"{idle} GPUs free"
    return 2, "1 GPU free"


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def atomic_write_status(path: Path | str, data: dict[str, Any]) -> None:
    """Atomic write: temp file → fsync → os.replace.

    Guarantees the reader either sees the previous snapshot or the new one,
    never a half-written file. Cleans up the .tmp file on any failure.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"), sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup, then re-raise so the caller can log it.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


_NVIDIA_SMI_CMD = [
    "nvidia-smi",
    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
    "--format=csv,noheader,nounits",
]
_SS_CMD = ["ss", "-tlnH"]


def _run_nvidia_smi(errors: list[str]) -> list[dict[str, Any]]:
    try:
        out = subprocess.check_output(
            _NVIDIA_SMI_CMD, text=True, timeout=5, stderr=subprocess.DEVNULL
        )
        return parse_nvidia_smi(out)
    except FileNotFoundError:
        errors.append("nvidia-smi not found")
    except subprocess.CalledProcessError as e:
        errors.append(f"nvidia-smi exit {e.returncode}")
    except subprocess.TimeoutExpired:
        errors.append("nvidia-smi timeout")
    except Exception as e:  # noqa: BLE001
        errors.append(f"nvidia-smi: {e!r}")
    return []


def _run_ss(errors: list[str]) -> list[dict[str, Any]]:
    try:
        out = subprocess.check_output(
            _SS_CMD, text=True, timeout=5, stderr=subprocess.DEVNULL
        )
        return parse_ss_tlnH(out)
    except FileNotFoundError:
        errors.append("ss not found")
    except subprocess.CalledProcessError as e:
        errors.append(f"ss exit {e.returncode}")
    except subprocess.TimeoutExpired:
        errors.append("ss timeout")
    except Exception as e:  # noqa: BLE001
        errors.append(f"ss: {e!r}")
    return []


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def collect_snapshot() -> dict[str, Any]:
    """Sample all sources once and return a status JSON snapshot."""
    errors: list[str] = []
    gpus = _run_nvidia_smi(errors)
    services = _run_ss(errors)

    vllm_info = probe_vllm(VLLM_BASE_URL, timeout=VLLM_PROBE_TIMEOUT_S)

    # Find vllm pid/port from `services` so we can enrich the vllm block.
    vllm_svc = next(
        (s for s in services if s.get("proc", "").startswith("vllm")), None
    )
    vllm_port = vllm_svc["port"] if vllm_svc else 8000
    vllm_pid = vllm_svc["pid"] if vllm_svc else None

    vllm_block = {
        "running": bool(vllm_info.get("running")),
        "model": vllm_info.get("model"),
        "port": vllm_port,
        "pid": vllm_pid,
        "uptime_s": None,  # not tracked in v1
    }

    tier, tier_reason = compute_tier(gpus, vllm_block)

    now = int(time.time())
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "host": socket.getfqdn(),
        "ts": now,
        "agent_uptime_s": int(time.time() - _AGENT_START_TS),
        "gpus": gpus,
        "vllm": vllm_block,
        "services": services,
        "tier": tier,
        "tier_reason": tier_reason,
        "inference_recent": None,  # vLLM /metrics not wired in v1
        "errors": errors,
    }
    return snapshot


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> int:
    print(
        f"rocco-agent: poll every {POLL_INTERVAL_S}s -> {STATUS_PATH}",
        file=sys.stderr,
        flush=True,
    )
    while True:
        try:
            snap = collect_snapshot()
            atomic_write_status(STATUS_PATH, snap)
        except Exception as e:  # noqa: BLE001 — never let the daemon die
            print(f"rocco-agent: sample failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
