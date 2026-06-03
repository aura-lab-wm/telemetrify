"""Unit tests for ClaudeCLIBackend (headless `claude -p`).

Strategy: monkeypatch `subprocess.run` inside the backend module to capture
the argv + stdin and return a canned `CompletedProcess`, plus monkeypatch
`_resolve_bin` so the tests never depend on a real `claude` install. Verifies:
  - the envelope's `result` becomes raw_text; usage tokens are parsed
  - argv shape: `-p --output-format json --model <m> --max-turns 1
    --append-system-prompt <system>`, user text on stdin
  - a dated model id (claude-haiku-4-5-20251001) is collapsed to the
    CLI-friendly alias the binary accepts (claude-haiku-4-5)
  - non-zero exit / timeout / error-envelope all raise BackendTransient so
    the router falls through to the next tier instead of dying
  - is_available() is True iff a binary resolves, and is cached
"""
from __future__ import annotations

import subprocess

import pytest


def _envelope(result: str = '{"ok": true}', in_tok: int = 10, out_tok: int = 5,
              *, is_error: bool = False, subtype: str = "success") -> str:
    import json
    return json.dumps({
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    })


def _patch_run(monkeypatch, *, stdout="", returncode=0, raises=None, capture=None):
    from telemetrify.ai.backends import claude_cli as mod

    def fake_run(argv, **kwargs):
        if capture is not None:
            capture["argv"] = argv
            capture["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    # Pretend a binary exists so complete()/is_available() don't touch the FS.
    monkeypatch.setattr(mod, "_resolve_bin", lambda: "/fake/bin/claude")


def test_complete_parses_envelope_result_and_usage(monkeypatch):
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend

    _patch_run(monkeypatch, stdout=_envelope('{"semantic_query": "x"}', 12, 34))
    b = ClaudeCLIBackend()
    resp = b.complete(system="be terse", user="who am I",
                      model="claude-haiku-4-5", max_tokens=400, json_schema={"x": 1})

    assert resp.raw_text == '{"semantic_query": "x"}'
    assert resp.input_tokens == 12
    assert resp.output_tokens == 34


def test_argv_shape_and_user_on_stdin(monkeypatch):
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend

    cap: dict = {}
    _patch_run(monkeypatch, stdout=_envelope(), capture=cap)
    b = ClaudeCLIBackend()
    b.complete(system="SYS", user="USER-TEXT", model="claude-sonnet-4-6",
               max_tokens=64, json_schema=None)

    argv = cap["argv"]
    assert argv[0] == "/fake/bin/claude"
    assert "-p" in argv
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--max-turns" in argv
    # system goes via --append-system-prompt, user via stdin
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert cap["kwargs"].get("input") == "USER-TEXT"


def test_complete_disables_capture_hook_in_subprocess_env(monkeypatch):
    """The spawned `claude -p` must NOT be captured into telemetrify's own
    corpus (the Stop hook would fire + run a nested grade). We pass
    TELEMETRIFY_NO_CAPTURE=1 in the child env (merged onto os.environ, so PATH
    / auth survive) and the capture entrypoint bails on it."""
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend

    cap: dict = {}
    _patch_run(monkeypatch, stdout=_envelope(), capture=cap)
    b = ClaudeCLIBackend()
    b.complete(system="s", user="u", model="m", max_tokens=8, json_schema=None)

    env = cap["kwargs"].get("env") or {}
    assert env.get("TELEMETRIFY_NO_CAPTURE") == "1"
    assert "PATH" in env, "child env must inherit the parent env, not replace it"


def test_dated_model_id_is_collapsed_to_alias(monkeypatch):
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend

    cap: dict = {}
    _patch_run(monkeypatch, stdout=_envelope(), capture=cap)
    b = ClaudeCLIBackend()
    b.complete(system="s", user="u", model="claude-haiku-4-5-20251001",
               max_tokens=64, json_schema=None)

    argv = cap["argv"]
    model_arg = argv[argv.index("--model") + 1]
    assert model_arg == "claude-haiku-4-5", \
        "trailing -YYYYMMDD must be stripped so the CLI doesn't 404"


def test_short_timeout_does_not_shrink_below_floor(monkeypatch):
    """A short per-call timeout (e.g. the planner's 20s) must NOT shrink the
    subprocess timeout below the CLI's own floor — the CLI is inherently slow,
    so a healthy call would otherwise be spuriously killed."""
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend

    cap: dict = {}
    _patch_run(monkeypatch, stdout=_envelope(), capture=cap)
    b = ClaudeCLIBackend(timeout_s=120)
    b.complete(system="s", user="u", model="m", max_tokens=8,
               json_schema=None, timeout=5)
    assert cap["kwargs"]["timeout"] >= 120


def test_nonzero_exit_raises_transient(monkeypatch):
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend
    from telemetrify.ai.backends.base import BackendTransient

    _patch_run(monkeypatch, stdout="", returncode=1)
    b = ClaudeCLIBackend()
    with pytest.raises(BackendTransient):
        b.complete(system="s", user="u", model="m", max_tokens=64, json_schema=None)


def test_timeout_raises_transient(monkeypatch):
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend
    from telemetrify.ai.backends.base import BackendTransient

    _patch_run(monkeypatch,
               raises=subprocess.TimeoutExpired(cmd="claude", timeout=1.0))
    b = ClaudeCLIBackend(timeout_s=1.0)
    with pytest.raises(BackendTransient):
        b.complete(system="s", user="u", model="m", max_tokens=64, json_schema=None)


def test_error_envelope_raises_transient(monkeypatch):
    from telemetrify.ai.backends.claude_cli import ClaudeCLIBackend
    from telemetrify.ai.backends.base import BackendTransient

    _patch_run(monkeypatch,
               stdout=_envelope("rate limited", is_error=True, subtype="error_max_turns"))
    b = ClaudeCLIBackend()
    with pytest.raises(BackendTransient):
        b.complete(system="s", user="u", model="m", max_tokens=64, json_schema=None)


def test_is_available_true_when_bin_resolves_and_caches(monkeypatch):
    from telemetrify.ai.backends import claude_cli as mod

    calls = {"n": 0}

    def fake_resolve():
        calls["n"] += 1
        return "/fake/bin/claude"

    monkeypatch.setattr(mod, "_resolve_bin", fake_resolve)
    b = mod.ClaudeCLIBackend()
    assert b.is_available() is True
    assert b.is_available() is True
    assert calls["n"] == 1, "is_available() must cache the probe"


def test_is_available_false_when_no_bin(monkeypatch):
    from telemetrify.ai.backends import claude_cli as mod

    monkeypatch.setattr(mod, "_resolve_bin", lambda: None)
    b = mod.ClaudeCLIBackend()
    assert b.is_available() is False
