"""ClaudeCLIBackend — shells out to the headless `claude -p` CLI.

Why this tier exists: when Rocco (vLLM) is down and the Mac-local Ollama tier
returns prose instead of JSON, the /ask PLANNER — which needs a strict JSON
object — fails deterministically (the router does NOT fall through on a bad-JSON
parse). The most reliable JSON source then is the user's own logged-in Claude
Code subscription, driven non-interactively. No API key required: the CLI reuses
the Keychain OAuth that Claude Code already holds, so it keeps working even when
the launchd-spawned UI process has an empty `ANTHROPIC_*` env.

Invocation (user text on stdin):
    claude -p --output-format json --model <m> --max-turns 1 \
           --append-system-prompt <system> --safe-mode --tools ""

--safe-mode and --tools "" isolate the call from the user's global ~/.claude
config (hooks, skills, CLAUDE.md, MCP servers, builtin tools) — without them
the spawned session is a full agentic Claude Code session that can hang
indefinitely on OAuth-gated MCP server setup or a headless permission prompt
with no TTY to answer it (2026-07-04: an /ask planner call died at exactly
the 120s subprocess timeout to this, not slow inference). --safe-mode keeps
normal keychain OAuth auth working, unlike --bare which forces
ANTHROPIC_API_KEY-only auth.

The CLI prints a one-line JSON envelope:
    {"subtype":"success","is_error":false,"result":"<text>",
     "usage":{"input_tokens":N,"output_tokens":N},"total_cost_usd":...}
We return envelope["result"] as raw_text; the router strips fences and extracts
the JSON object when a schema is supplied.

Recoverable failures (binary missing, non-zero exit, timeout, error envelope)
raise BackendTransient so the router falls through to the next tier (anthropic)
rather than killing the request.

Env knobs:
  CLAUDE_CLI_BIN        explicit path to the claude binary (skips PATH lookup)
  CLAUDE_CLI_TIMEOUT_S  subprocess timeout, seconds (default 120)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .base import BackendResponse, BackendTransient

# How long to trust a successful is_available() probe. Seconds.
_AVAIL_CACHE_TTL_S = 30.0

# Fallback locations checked when `claude` isn't on PATH. The Homebrew symlink
# is what a launchd process (PATH=/opt/homebrew/bin:...) actually resolves.
_KNOWN_PATHS = (
    "/opt/homebrew/bin/claude",
    os.path.expanduser("~/.local/bin/claude"),
    "/usr/local/bin/claude",
)

# Trailing -YYYYMMDD date stamp the CLI's --model often rejects (it wants the
# undated alias, e.g. claude-haiku-4-5). Strip it.
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _log(msg: str) -> None:
    """Cheap diagnostic line to stderr — same convention as ai/router.py's
    `_log` (grep '[claude_cli]' data/ui-stderr.log)."""
    import sys
    print(f"[claude_cli] {msg}", file=sys.stderr, flush=True)


def _augment_system_prompt(system: str, json_schema: Any | None) -> str:
    """BUG 5 (2026-07 audit): json_schema had no effect at all on this tier
    — the CLI has no guided-decoding equivalent of vLLM's structured output
    API, so there's no wire-level way to enforce a shape. The one lever we
    do have is a plain-text steer appended to the system prompt. This is
    best-effort only (nothing enforces it at decode time — the router's
    post-hoc schema validation is still what actually catches a bad shape),
    but it's strictly better than silently ignoring the hint entirely."""
    if not json_schema:
        return system
    try:
        import json as _json
        hint = _json.dumps(json_schema)
    except Exception:
        return system
    return (
        f"{system}\n\nYour entire response MUST be a single JSON object "
        f"matching this shape (field: {{type, constraints}}): {hint}\n"
        f"Return ONLY that JSON object — no prose, no markdown code fences."
    )


def _resolve_bin() -> str | None:
    """Locate the real `claude` executable (never the shell-function wrapper)."""
    env_bin = os.environ.get("CLAUDE_CLI_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    which = shutil.which("claude")
    if which:
        return which
    for p in _KNOWN_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _map_model(model: str) -> str:
    if not model:
        return "sonnet"
    return _DATE_SUFFIX_RE.sub("", model)


class ClaudeCLIBackend:
    name = "claude_cli"
    # Subscription-driven: the marginal cost to the user is ~0, so the ai_runs
    # dashboard shows $0 for this tier (the router multiplies tokens by these).
    input_price_per_m = 0.0
    output_price_per_m = 0.0

    def __init__(self, *, timeout_s: float | None = None) -> None:
        if timeout_s is None:
            try:
                timeout_s = float(os.environ.get("CLAUDE_CLI_TIMEOUT_S", "120"))
            except ValueError:
                timeout_s = 120.0
        self.request_timeout_s = timeout_s
        self._avail_cache: tuple[float, bool] | None = None  # (expires_at, value)

    # ── availability probe (cached) ─────────────────────────────────────
    def is_available(self) -> bool:
        now = time.monotonic()
        if self._avail_cache is not None and self._avail_cache[0] > now:
            return self._avail_cache[1]
        ok = _resolve_bin() is not None
        self._avail_cache = (now + _AVAIL_CACHE_TTL_S, ok)
        return ok

    # ── core call ────────────────────────────────────────────────────────
    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        json_schema: Any | None,
        timeout: float | None = None,
    ) -> BackendResponse:
        bin_path = _resolve_bin()
        if bin_path is None:
            raise BackendTransient(
                "claude CLI not found (set CLAUDE_CLI_BIN or add it to PATH)")

        eff_model = _map_model(model)
        argv = [
            bin_path, "-p",
            "--output-format", "json",
            "--model", eff_model,
            "--max-turns", "1",
            "--append-system-prompt", _augment_system_prompt(system, json_schema),
            # Isolate from the user's global ~/.claude config — see the module
            # docstring above for the full rationale (2026-07-04 hang incident).
            "--safe-mode",
            "--tools", "",
        ]

        # Run in a neutral cwd so the CLI doesn't load telemetrify's own
        # project-level .claude hooks / CLAUDE.md — cheaper, and any turn the
        # user-level capture hook records lands under a throwaway dir instead
        # of polluting THIS project's corpus.
        workdir: str | None = os.path.join(
            tempfile.gettempdir(), "telemetrify-claude-cli")
        try:
            os.makedirs(workdir, exist_ok=True)
        except Exception:
            workdir = None

        # Disable telemetrify's own capture in the spawned session: cwd above
        # only dodges PROJECT-level hooks, but the user-level ~/.claude Stop
        # hook would still fire — capturing this internal call into the real
        # corpus AND running a nested LLM grade. The capture entrypoint bails
        # when it sees this flag. Merge onto os.environ so PATH + Keychain auth
        # survive.
        child_env = {**os.environ, "TELEMETRIFY_NO_CAPTURE": "1"}
        # The CLI is inherently slow (it loads the full Claude Code system
        # prompt each call). Never shrink below our own floor — a short caller
        # timeout (e.g. the planner's 20s) would spuriously kill a healthy call.
        eff_timeout = max(float(timeout or 0.0), self.request_timeout_s)
        # BUG 3 (2026-07 audit): that floor used to raise a caller's timeout
        # silently. Keep the floor (real reason: genuinely slow cold-start —
        # see module docstring), but make the override visible so a caller
        # expecting e.g. 20s and actually waiting 120s isn't a silent
        # surprise during debugging.
        if timeout is not None and eff_timeout > timeout:
            _log(
                f"requested timeout={timeout}s raised to floor "
                f"{self.request_timeout_s}s (effective={eff_timeout}s)"
            )
        try:
            proc = subprocess.run(
                argv,
                input=user,
                capture_output=True,
                text=True,
                timeout=eff_timeout,
                cwd=workdir,
                env=child_env,
            )
        except subprocess.TimeoutExpired as e:
            raise BackendTransient(
                f"claude CLI timed out after {eff_timeout}s") from e
        except (FileNotFoundError, OSError) as e:
            raise BackendTransient(f"claude CLI exec failed: {e}") from e

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise BackendTransient(
                f"claude CLI exit {proc.returncode}: {tail or '(no output)'}")

        out = (proc.stdout or "").strip()
        try:
            env = json.loads(out)
        except Exception as e:
            raise BackendTransient(
                f"claude CLI non-JSON envelope: {out[:200]!r}") from e

        if env.get("is_error") or env.get("subtype") not in (None, "success"):
            raise BackendTransient(
                f"claude CLI error envelope: subtype={env.get('subtype')!r} "
                f"{str(env.get('result', ''))[:200]}")

        raw_text = env.get("result")
        if not isinstance(raw_text, str):
            raise BackendTransient("claude CLI envelope missing string 'result'")

        usage = env.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)

        return BackendResponse(
            raw_text=raw_text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=eff_model,
            # BUG 4: the `-p --output-format json` envelope has no per-call
            # finish/stop-reason field (subtype=="success" only tells us the
            # overall agent turn didn't error; it doesn't distinguish a
            # naturally-finished completion from one truncated by the
            # model's own max output tokens). Left None rather than guess —
            # unlike the other two tiers, this one genuinely has no signal
            # to surface here.
            stop_reason=None,
        )
