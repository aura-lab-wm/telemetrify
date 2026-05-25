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

SCHEMA_VERSION = 4  # v4: top-level models{} block (selected_profile + available[])
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

# Optional integration with the on-host `model_manager` project. When this
# project is present we use it as the AUTHORITATIVE source for vLLM
# liveness, configured model id, and tier — because that's what every
# other consumer on the host already reads (the prompt-submit hook, etc.).
# When it's absent we silently fall back to the curl-the-port path so the
# agent still works on a vanilla GPU box that doesn't have the manager.
MANAGER_ROOT = Path(
    os.environ.get("ROCCO_MANAGER_ROOT", "/scratch/amastropaolo/rocco-inference")
)
MANAGER_PYTHON = Path(
    os.environ.get(
        "ROCCO_MANAGER_PYTHON",
        str(MANAGER_ROOT / ".venv" / "bin" / "python"),
    )
)
MANAGER_PROBE_TIMEOUT_S = 3.0

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
# Service classification + enrichment (schema v2)
# ---------------------------------------------------------------------------
#
# Each classifier is (matcher, kind). Matcher receives (port, proc, command)
# and returns True if this service fits the kind. First match wins, so order
# matters — list more specific patterns before broad ones.
#
# `kind` values the Mac client knows how to render:
#   vllm           — OpenAI-compatible inference server (port 8000-8099)
#   ollama         — Ollama daemon (default port 11434)
#   jupyter        — Jupyter Lab / Notebook
#   telemetrify    — telemetrify FastAPI UI (port 8765-8767 by convention)
#   ssh            — sshd
#   prometheus     — node_exporter / vllm metrics
#   nfs-portmap    — rpcbind / portmap
#   dns-stub       — systemd-resolved local stub
#   unknown        — anything else; UI shows the raw port + command

_SERVICE_CLASSIFIERS: list[tuple[Any, str]] = [
    (lambda port, proc, cmd: proc.startswith("vllm"), "vllm"),
    (lambda port, proc, cmd: proc.startswith("ollama") or port == 11434, "ollama"),
    (lambda port, proc, cmd: "jupyter" in cmd.lower() or port in (8888, 8889, 8890), "jupyter"),
    (lambda port, proc, cmd: "telemetrify" in cmd.lower() or "uvicorn" in cmd.lower(), "telemetrify"),
    (lambda port, proc, cmd: proc == "sshd" or port == 22, "ssh"),
    (lambda port, proc, cmd: port == 9100 or proc.startswith("node_export"), "prometheus"),
    (lambda port, proc, cmd: port == 111, "nfs-portmap"),
    (lambda port, proc, cmd: port == 53, "dns-stub"),
    # vLLM uses 8000-8099 even if the proc name didn't make it through (e.g.
    # the launcher's python -m vllm ...): treat that port range as vllm by
    # default. Listed late so an explicitly-named ollama on 8000 still wins.
    (lambda port, proc, cmd: 8000 <= port < 8100, "vllm"),
    # telemetrify FastAPI on its conventional ports (8765, 8767), even if
    # the process name came through as plain "python".
    (lambda port, proc, cmd: port in (8765, 8766, 8767), "telemetrify"),
]


def classify_service(port: int, proc: str, command: str) -> str:
    """Return the `kind` string for a (port, proc, command) tuple."""
    for matcher, kind in _SERVICE_CLASSIFIERS:
        try:
            if matcher(port, proc, command):
                return kind
        except Exception:  # noqa: BLE001 — never let a matcher crash the daemon
            continue
    return "unknown"


def _read_proc_text(pid: int, name: str) -> str:
    """Read `/proc/<pid>/<name>`; return "" on any error."""
    try:
        with open(f"/proc/{pid}/{name}", "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def _resolve_uid_to_user(uid: int) -> str:
    """Map a numeric uid to a login name. Fall back to the numeric form."""
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return str(uid)


def lookup_proc_owner(pid: int) -> dict[str, str]:
    """Look up `command` (from /proc/<pid>/cmdline) and `user` (from
    /proc/<pid>/status `Uid:` line, mapped via pwd) for a PID.

    Returns {"command": str, "user": str}. Empty strings on lookup failure
    so the caller can still emit a row even when /proc is unreadable
    (containers, restricted namespaces).
    """
    cmdline_raw = _read_proc_text(pid, "cmdline")
    # cmdline is NUL-separated argv; join with spaces for display.
    command = " ".join(cmdline_raw.split("\x00")).strip()
    # Trim very long argv (training scripts can have huge --args) so the
    # JSON snapshot stays small.
    if len(command) > 240:
        command = command[:239] + "…"

    user = ""
    status = _read_proc_text(pid, "status")
    for line in status.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            # `Uid: <real> <effective> <saved> <fs>` — use effective.
            if len(parts) >= 3:
                try:
                    user = _resolve_uid_to_user(int(parts[2]))
                except ValueError:
                    user = ""
            break
    return {"command": command, "user": user}


def enrich_services(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mutate each service row in place to add `kind`, `command`, `user`.

    Schema v1 callers (older Mac clients) just ignore the new fields —
    they're additive on a Codable struct with optional properties.
    """
    for svc in services:
        pid = svc.get("pid")
        owner = (lookup_proc_owner(pid) if isinstance(pid, int) else
                 {"command": "", "user": ""})
        svc["command"] = owner["command"]
        svc["user"] = owner["user"]
        svc["kind"] = classify_service(
            port=int(svc.get("port") or 0),
            proc=str(svc.get("proc") or ""),
            command=owner["command"],
        )
    return services


# ---------------------------------------------------------------------------
# HTTP banner probe — gives the AI classifier something to chew on
# ---------------------------------------------------------------------------

# Cache so we don't re-probe a port on every 5s tick. Most services don't
# change their banner. Mapped: port → (probe_text, written_at_epoch).
_PROBE_CACHE: dict[int, tuple[str, float]] = {}
_PROBE_TTL_S = 60.0
_PROBE_MAX_PER_TICK = 10  # cap so a host with 30 unknown ports doesn't stall
_PROBE_TIMEOUT_S = 1.0
_PROBE_MAX_BYTES = 240   # first ~3 header lines is plenty for the LLM


def probe_http_banner(port: int, timeout: float = _PROBE_TIMEOUT_S) -> str:
    """HEAD-style probe on localhost:port. Returns a short string the
    AI classifier can use to guess what the service is.

    Returns one of:
      - "HTTP/1.1 200 ...\\nContent-Type: application/json\\n..." (first
        ~240 bytes of response headers for an HTTP service)
      - "non-http: <reason>" when the port isn't HTTP (empty reply,
        connection refused after connect, binary protocol like ZMQ, …)
      - "" when even the connect failed
    """
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}",
            method="HEAD",
            headers={"User-Agent": "rocco-agent/probe"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            headers = []
            for k, v in (resp.headers.items() if resp.headers else []):
                line = f"{k}: {v}"
                headers.append(line)
                if sum(len(h) for h in headers) > _PROBE_MAX_BYTES:
                    break
            text = f"HTTP/1.1 {status}\n" + "\n".join(headers)
            return text[:_PROBE_MAX_BYTES]
    except urllib.error.HTTPError as e:
        # Real HTTP server that returned non-2xx — that's still useful
        # signal (e.g. 403 Forbidden, 404 Not Found, 401 Unauthorized).
        try:
            headers = list((e.headers.items() if e.headers else []))[:5]
            text = f"HTTP/1.1 {e.code}\n" + "\n".join(f"{k}: {v}" for k, v in headers)
            return text[:_PROBE_MAX_BYTES]
        except Exception:
            return f"HTTP/1.1 {e.code}"
    except urllib.error.URLError as e:
        # ConnectionRefused / Network unreachable: probably the port has
        # closed since `ss` saw it (transient). Or it's a binary protocol
        # that hung up on our HTTP request (ZMQ, gRPC, redis, postgres).
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        return f"non-http: {reason}"[:_PROBE_MAX_BYTES]
    except (TimeoutError, OSError) as e:
        return f"non-http: {e!r}"[:_PROBE_MAX_BYTES]
    except Exception as e:  # noqa: BLE001 — never crash the daemon
        return f"non-http: probe error {e!r}"[:_PROBE_MAX_BYTES]


def enrich_unknown_probes(services: list[dict[str, Any]]) -> None:
    """For every service whose kind is 'unknown', attach a `probe` field
    with whatever the port responds to a HEAD request. Cached for
    _PROBE_TTL_S so we don't re-probe every tick.

    Only probes up to _PROBE_MAX_PER_TICK ports per call — a host with
    dozens of unknowns shouldn't make the poll cycle take 30 seconds.
    Subsequent ticks naturally cover the rest.
    """
    import concurrent.futures as _cf

    now = time.time()
    needs_probing: list[dict[str, Any]] = []
    for svc in services:
        if svc.get("kind") != "unknown":
            continue
        port = svc.get("port")
        if not isinstance(port, int):
            continue
        cached = _PROBE_CACHE.get(port)
        if cached and (now - cached[1]) < _PROBE_TTL_S:
            svc["probe"] = cached[0]
            continue
        needs_probing.append(svc)

    if not needs_probing:
        return

    # Parallel probe, capped, 1s timeout each. ThreadPool is fine for
    # network IO; ~10 probes in ~1s.
    batch = needs_probing[:_PROBE_MAX_PER_TICK]
    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(probe_http_banner, svc["port"]): svc for svc in batch}
        for fut in _cf.as_completed(futures, timeout=_PROBE_TIMEOUT_S * 2):
            svc = futures[fut]
            try:
                banner = fut.result()
            except Exception:  # noqa: BLE001
                banner = ""
            _PROBE_CACHE[svc["port"]] = (banner, now)
            svc["probe"] = banner


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


def probe_via_manager(
    project_root: Path = MANAGER_ROOT,
    python: Path = MANAGER_PYTHON,
    timeout: float = MANAGER_PROBE_TIMEOUT_S,
) -> dict[str, Any] | None:
    """Authoritative vLLM/state probe via `python -m model_manager.manager status`.

    Returns a dict with at minimum:
      {"running": bool, "model": str|None, "port": int|None,
       "model_id": str|None, "description": str|None}
    OR `None` if the manager isn't installed / failed / returned bad JSON
    — in which case the caller should fall back to the curl probe so the
    agent still works on hosts that don't have the manager project.

    We trust the manager over `curl localhost:8000` because EVERY other
    consumer on the host (Mac prompt-submit hooks, CI, the human running
    `manager status` interactively) reads from the same source — single
    source of truth means no more "the hook says RUNNING but the popover
    says offline" divergence.
    """
    if not python.exists() or not project_root.exists():
        return None
    try:
        result = subprocess.run(
            [str(python), "-m", "model_manager.manager", "status"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    state = payload.get("state") or {}
    running = bool(state.get("vllm_running"))
    full_id = state.get("model_id") or ""
    # "moonshotai/Kimi-Dev-72B" → "Kimi-Dev-72B" for compact display.
    short_model = full_id.rsplit("/", 1)[-1] if full_id else None
    # `tier` from the manager is its authoritative readiness signal —
    # accounts for in-use memory + reservations + the manager's own
    # logic. Our local `compute_tier()` heuristic is naive (util<5%)
    # and disagreed with the manager when other lab users had weights
    # loaded but were CPU-idle for a moment — surfaced as "Tier 4 ·
    # 4 GPUs free" in the menubar while the prompt-submit hook
    # reading the SAME manager said tier 0 (no GPUs free). Surface
    # the manager's number so both reads agree.
    free_gpus = state.get("free_gpus") or []
    return {
        "running": running,
        "model": short_model if running else None,
        # When vLLM is offline we still know the CONFIGURED model — surface
        # it on a separate key so the menubar can render
        # "vLLM offline · configured: Kimi-Dev-72B" instead of "idle".
        "configured_model": short_model,
        "port": state.get("vllm_port"),
        "description": state.get("description"),
        # Authoritative tier — caller falls back to compute_tier()
        # when these are None (i.e. manager not installed).
        "tier": state.get("tier"),
        "tier_reason": state.get("description"),
        "free_gpus": free_gpus,
        # Model selection (manager schema gained these): which profile is
        # pinned ("auto" or 1..4) and the pinnable configs the menubar
        # renders in its picker. Pass through verbatim.
        "selected_profile": state.get("selected_profile"),
        "available_models": state.get("available_models"),
    }


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
    services = enrich_services(_run_ss(errors))
    # Tag each unknown listener with an HTTP banner (when it speaks
    # HTTP) — gives the Mac's AI classifier the only signal it has
    # for ports that ss couldn't attach a pid/command to.
    enrich_unknown_probes(services)

    # Prefer the on-host model_manager when it's installed — that's what
    # every other consumer of "is vLLM up?" already reads, so we agree
    # with them by construction. Fall back to the HTTP probe when it's not
    # available (e.g. on a vanilla GPU host without rocco-inference).
    manager_info = probe_via_manager()
    vllm_info = manager_info if manager_info is not None else \
        probe_vllm(VLLM_BASE_URL, timeout=VLLM_PROBE_TIMEOUT_S)

    # Find vllm pid/port from `services` so we can enrich the vllm block.
    vllm_svc = next(
        (s for s in services if s.get("proc", "").startswith("vllm")), None
    )
    vllm_port = vllm_svc["port"] if vllm_svc else (
        vllm_info.get("port") or 8000
    )
    vllm_pid = vllm_svc["pid"] if vllm_svc else None

    vllm_block = {
        "running": bool(vllm_info.get("running")),
        "model": vllm_info.get("model"),
        "port": vllm_port,
        "pid": vllm_pid,
        "uptime_s": None,  # not tracked in v1
    }
    # When vLLM is offline but a model is *configured*, surface that on a
    # separate optional key so the menubar can render
    # "vLLM offline · configured: Kimi-Dev-72B" instead of bland "idle".
    configured = vllm_info.get("configured_model")
    if configured and not vllm_block["running"]:
        vllm_block["configured_model"] = configured

    # Tier — prefer the on-host model_manager's authoritative value
    # (same source the prompt-submit hook reads, so menubar & hook
    # agree by construction). Fall back to our local naive heuristic
    # only when the manager isn't installed.
    mgr_tier = vllm_info.get("tier") if isinstance(vllm_info, dict) else None
    mgr_reason = vllm_info.get("tier_reason") if isinstance(vllm_info, dict) else None
    if isinstance(mgr_tier, int):
        tier = mgr_tier
        tier_reason = mgr_reason or compute_tier(gpus, vllm_block)[1]
    else:
        tier, tier_reason = compute_tier(gpus, vllm_block)

    # Model-selection block (schema v4): the pinned profile + the pinnable
    # configs the menubar's picker renders. Only present when the manager
    # surfaced them; older managers / the curl fallback leave it empty.
    models_block = {
        "selected_profile": vllm_info.get("selected_profile")
        if isinstance(vllm_info, dict) else None,
        "available": (vllm_info.get("available_models") or [])
        if isinstance(vllm_info, dict) else [],
    }

    now = int(time.time())
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "host": socket.getfqdn(),
        "ts": now,
        "agent_uptime_s": int(time.time() - _AGENT_START_TS),
        "gpus": gpus,
        "vllm": vllm_block,
        "models": models_block,
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
