"""Tests for the Rocco status agent.

Loads the agent module from ``menubar/rocco-agent/rocco-agent.py`` even though
the filename has a hyphen (not a valid identifier).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import URLError

import pytest

HERE = Path(__file__).resolve().parent
AGENT_DIR = HERE.parent
FIXTURES = HERE / "fixtures"


def _load_agent_module():
    src = AGENT_DIR / "rocco-agent.py"
    spec = importlib.util.spec_from_file_location("rocco_agent", src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rocco_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def agent():
    return _load_agent_module()


@pytest.fixture
def nvidia_csv() -> str:
    return (FIXTURES / "nvidia-smi.csv").read_text()


@pytest.fixture
def ss_output() -> str:
    return (FIXTURES / "ss-tlnH.txt").read_text()


# ---------------------------------------------------------------------------
# parse_nvidia_smi
# ---------------------------------------------------------------------------


def test_parse_nvidia_smi_parses_three_gpus(agent, nvidia_csv):
    gpus = agent.parse_nvidia_smi(nvidia_csv)
    assert len(gpus) == 3
    expected_keys = {
        "idx",
        "name",
        "util_pct",
        "mem_used_mib",
        "mem_total_mib",
        "temp_c",
        "power_w",
    }
    for g in gpus:
        assert set(g.keys()) == expected_keys

    assert gpus[0]["idx"] == 0
    assert gpus[0]["name"] == "NVIDIA A100-SXM4-80GB"
    assert gpus[0]["util_pct"] == 87
    assert gpus[0]["mem_used_mib"] == 72341
    assert gpus[0]["mem_total_mib"] == 81920
    assert gpus[0]["temp_c"] == 71
    assert gpus[0]["power_w"] == pytest.approx(312.45)

    assert gpus[1]["idx"] == 1
    assert gpus[1]["util_pct"] == 2
    assert gpus[2]["util_pct"] == 0
    assert gpus[2]["mem_used_mib"] == 4


# ---------------------------------------------------------------------------
# parse_ss_tlnH
# ---------------------------------------------------------------------------


def test_parse_ss_tlnH_finds_vllm_and_sshd(agent, ss_output):
    services = agent.parse_ss_tlnH(ss_output)
    by_port = {(s["port"], s["proc"]): s for s in services}

    assert (8000, "vllm") in by_port
    assert (22, "sshd") in by_port

    vllm = by_port[(8000, "vllm")]
    assert vllm["pid"] == 1234

    sshd = by_port[(22, "sshd")]
    assert sshd["pid"] == 987


# ---------------------------------------------------------------------------
# probe_vllm
# ---------------------------------------------------------------------------


def test_probe_vllm_running_when_models_endpoint_returns_200(agent):
    body = json.dumps(
        {"object": "list", "data": [{"id": "Kimi-Dev-72B", "object": "model"}]}
    ).encode()

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = body
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = agent.probe_vllm("http://localhost:8000", timeout=1.0)

    assert result["running"] is True
    assert result["model"] == "Kimi-Dev-72B"


def test_probe_vllm_not_running_when_urlerror(agent):
    with patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
        result = agent.probe_vllm("http://localhost:8000", timeout=1.0)
    assert result["running"] is False
    assert result["model"] is None


# ---------------------------------------------------------------------------
# compute_tier
# ---------------------------------------------------------------------------


def test_compute_tier_four_idle_gpus_vllm_down(agent):
    gpus = [
        {"idx": 0, "util_pct": 0, "mem_used_mib": 4, "mem_total_mib": 81920},
        {"idx": 1, "util_pct": 1, "mem_used_mib": 8, "mem_total_mib": 81920},
        {"idx": 2, "util_pct": 4, "mem_used_mib": 12, "mem_total_mib": 81920},
        {"idx": 3, "util_pct": 2, "mem_used_mib": 16, "mem_total_mib": 81920},
    ]
    vllm = {"running": False, "model": None}
    tier, reason = agent.compute_tier(gpus, vllm)
    assert tier == 4
    assert reason == "4 GPUs free"


def test_compute_tier_zero_idle_gpus_yields_tier_one(agent):
    gpus = [
        {"idx": 0, "util_pct": 80, "mem_used_mib": 0, "mem_total_mib": 81920},
        {"idx": 1, "util_pct": 75, "mem_used_mib": 0, "mem_total_mib": 81920},
    ]
    vllm = {"running": True, "model": "Kimi-Dev-72B"}
    tier, _reason = agent.compute_tier(gpus, vllm)
    assert tier == 1


# ---------------------------------------------------------------------------
# atomic_write_status
# ---------------------------------------------------------------------------


def test_atomic_write_status_no_tmp_lingers_and_roundtrips(agent, tmp_path):
    target = tmp_path / "rocco-status.json"
    payload = {"schema_version": 1, "tier": 3, "gpus": []}

    agent.atomic_write_status(target, payload)

    assert target.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"unexpected .tmp files: {leftovers}"

    loaded = json.loads(target.read_text())
    assert loaded == payload


# ---------------------------------------------------------------------------
# collect_snapshot
# ---------------------------------------------------------------------------


def test_collect_snapshot_returns_full_schema(agent, nvidia_csv, ss_output):
    body = json.dumps({"data": [{"id": "Kimi-Dev-72B"}]}).encode()
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = body
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    def fake_check_output(cmd, *args, **kwargs):
        # cmd is a list — first arg names the tool.
        head = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
        if "nvidia-smi" in head:
            return nvidia_csv
        if "ss" in head:
            return ss_output
        raise AssertionError(f"unexpected subprocess: {cmd!r}")

    with patch.object(agent.subprocess, "check_output", side_effect=fake_check_output), \
         patch("urllib.request.urlopen", return_value=fake_resp), \
         patch.object(agent.socket, "getfqdn", return_value="rocco.cs.wm.edu"), \
         patch.object(agent.time, "time", return_value=1737759600.0):
        snap = agent.collect_snapshot()

    # Top-level schema keys
    expected_top = {
        "schema_version",
        "host",
        "ts",
        "agent_uptime_s",
        "gpus",
        "vllm",
        "services",
        "tier",
        "tier_reason",
        "inference_recent",
        "errors",
    }
    assert set(snap.keys()) == expected_top
    assert snap["schema_version"] == 1
    assert snap["host"] == "rocco.cs.wm.edu"
    assert snap["ts"] == 1737759600

    assert len(snap["gpus"]) == 3
    gpu_keys = {
        "idx",
        "name",
        "util_pct",
        "mem_used_mib",
        "mem_total_mib",
        "temp_c",
        "power_w",
    }
    for g in snap["gpus"]:
        assert gpu_keys.issubset(g.keys())

    assert snap["vllm"]["running"] is True
    assert snap["vllm"]["model"] == "Kimi-Dev-72B"

    ports = {s["port"] for s in snap["services"]}
    assert 8000 in ports and 22 in ports

    assert isinstance(snap["tier"], int)
    assert isinstance(snap["tier_reason"], str)
    assert snap["errors"] == []
