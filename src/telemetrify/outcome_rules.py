"""Config-driven, per-project outcome + evidence rules.

Two consumers:
  - `outcome_rules.match_output(project, tool_name, output_text)` → tag | None
    Used by the run_events stamping layer (#1/#3). Matches OUTPUT ONLY —
    never against input_json or a file's contents. A success marker that
    appears only in source code the agent READ/edited must never be counted
    as an outcome (the real bug that motivated this: the string
    "████ CREATED (replayed hold!)" appeared only in read source code across
    100+ runs and was repeatedly mistaken for a success).

  - `outcome_rules.evidence_assess(project, assistant_text, tool_results)` →
    (claim_present: bool, backed: bool) used by the evidence-backing grade
    (#2). `tool_results` is a list of (tool_name, output_text) tuples from
    the SAME turn's tool_calls; read-only tools (Read/Grep/Glob/…) are
    excluded by default so a success marker inside a file the agent merely
    READ can't back a claim — only a tool that PRODUCED that output can.

Config is JSON at `data/outcome_rules.json`; if absent, the built-in
defaults below ship. The file is hot-loaded (re-read each call) so the
operator can tune rules without restarting the UI / re-running migrations.
A schema error is logged to capture.log and the defaults are used (fail-open
— never break capture on a malformed config).
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Iterable

from . import DATA_DIR, LOG_PATH


# ─── Built-in defaults ───────────────────────────────────────────────────
# Outcome rules: regex matched (re.search, case-insensitive) against the
# tool RESULT output_text ONLY. `outcome` is 'success' | 'failure'. The
# first matching rule wins (order matters: put specific failures before
# generic successes, e.g. a compiler-error line before "✓ tests pass").
#
# These are intentionally conservative — they catch unambiguous, high-signal
# markers across many toolchains (git, pytest, build systems, cargo, npm,
# shell exit reporting). The operator is expected to layer per-project
# rules on top via data/outcome_rules.json.
DEFAULT_OUTCOME_RULES: list[dict] = [
    # ── Failures (checked first so they win over a trailing success line) ──
    # NB: failure patterns must NOT match a passing report — "0 failed" is a
    # success signal, so we require a nonzero count ([1-9]\d*) for "N failed".
    {"tag": "build_failed",   "outcome": "failure", "pattern": r"\berror:\s|\berror\[\d+\]|\bfatal error\b|\bFAIL\b|✗|❌"},
    {"tag": "tests_failed",   "outcome": "failure", "pattern": r"\b[1-9]\d* failed\b|tests? FAILED|\bFAILED\b.*\b(test|assert)"},
    {"tag": "command_error",  "outcome": "failure", "pattern": r"Command failed|exit code [1-9]\d*\b|exited with (code )?[1-9]"},
    {"tag": "merge_conflict", "outcome": "failure", "pattern": r"CONFLICT \(content\)|merge conflict|Automatic merge failed"},
    {"tag": "not_found",      "outcome": "failure", "pattern": r"No such file or directory|command not found"},
    # ── Successes ──
    {"tag": "tests_passed",   "outcome": "success", "pattern": r"\b\d+ passed\b.*\b0 failed\b|all tests? passed|✓.*\b(test|pass)|tests? PASSED|\b\d+ passed\b(?=.*\b0 failed)"},
    {"tag": "build_ok",       "outcome": "success", "pattern": r"Build Succeeded|BUILD SUCCEEDED|Compiling.*finished|\bFinished\b.*release"},
    {"tag": "pushed",         "outcome": "success", "pattern": r"-> main\b|-> master\b|\bHEAD -> \S+\s*$"},
    {"tag": "committed",      "outcome": "success", "pattern": r"\b\d+ files? changed\b|\[main [0-9a-f]{7,}\]"},
    {"tag": "installed",      "outcome": "success", "pattern": r"Successfully installed|Installed \d+ package"},
    {"tag": "deployed",       "outcome": "success", "pattern": r"Deployed|deployment (successful|complete)|→ (live|deployed)"},
    {"tag": "command_ok",     "outcome": "success", "pattern": r"exit code 0\b|exited with 0\b"},
]

# Tools whose output may legitimately back a success claim. Two classes:
#
#   EVIDENCE_PRODUCING_TOOLS — tools that REVEAL FACTS (grep matches, ls
#   listings, git log/diff, web search results). "Function foo exists" IS
#   legitimately backed by a `grep foo` result; "uses React" is backed by
#   `cat package.json`; "the previous commit introduced X" by `git log`.
#   The relevant property is "does the tool reveal facts?", NOT "does it
#   modify state?". (Reviewed against the operator's corpus — excluding
#   these was inflating the unsupported-claim rate by ignoring lookup-backed
#   factual claims.)
#
#   ACTOR_TOOLS — Bash, Edit, Write, NotebookEdit: tools whose result is a
#   success/failure marker of work the agent did (exit code 0, "updated
#   successfully", "File created successfully").
#
# Excluded by default:
#   - bare `Read` of arbitrary file CONTENTS — the motivating anti-false-
#     positive: a success marker ("████ CREATED (replayed hold!)") sitting in
#     source code the agent merely READ must not count as evidence. A Read
#     result is file *contents* (could be anything), not a fact the tool
#     produced. The operator can re-enable Read for a project via
#     data/outcome_rules.json if its corpus is clean.
#   - non-fact orchestration tools (TodoWrite, Task*, AskUserQuestion,
#     ToolSearch, Skill) — their output isn't evidence of an outcome.
ACTOR_TOOLS_DEFAULT = {"Bash", "Edit", "Write", "NotebookEdit"}
EVIDENCE_PRODUCING_TOOLS_DEFAULT = {
    "Grep", "Glob", "LS", "WebFetch", "WebSearch",
}
# Tools that are NEVER evidence (orchestration / planning / control). Read is
# listed separately from this set so it can be re-enabled independently.
NON_EVIDENCE_TOOLS_DEFAULT = {
    "Read",  # file *contents* — see module docstring; re-enable per-project if clean
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
    "AskUserQuestion", "ToolSearch", "Skill", "Task", "Agent", "Workflow",
    "Monitor", "ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
    "PushNotification", "SendMessage", "EnterPlanMode", "ExitPlanMode",
    "EnterWorktree", "ExitWorktree", "ReportFindings", "ListMcpResourcesTool",
}

# A success claim in assistant_text. The agent asserting completion — tuned
# to first-person / sentence-anchored phrasing so the bare word "done" or
# "works" appearing mid-sentence inside a longer explanation does NOT fire
# (that was the overfire driver at 70%). Hedged phrasing ("might have",
# "should have", "I think", "appears to", "seems to", "trying to") is
# excluded by the HEDGE lexicon below — a hedge downgrades an assertion.
CLAIM_LEXICON_DEFAULT = [
    # Sentence-start or "I've/I have/I" anchored completion assertions.
    r"(?m)^I (?:have |'ve )?(?:created|built|shipped|deployed|installed|applied|fixed|patched|added|written|wrote|updated|refactored|migrated|removed|deleted|set up|configured|implemented|finished|done|completed|resolved|verified)\b",
    r"(?m)^I'm done\b", r"(?m)^Done\b", r"(?m)^All (?:done|set|set up|good)\b",
    r"\bI (?:have |'ve )(?:verified|confirmed|checked|tested|validated)\b",
    r"\bverified that\b", r"\bconfirmed that\b", r"\bchecks? out\b",
    r"\b(?:it|this|that) (?:now )?works\b", r"\bit's working\b",
    r"\btests? passed\b", r"\ball tests? pass(ed)?\b", r"\bsuccess(?:fully)?\b",
    # Explicit success markers.
    r"✅", r"✓", r"🏁",
]
HEDGE_LEXICON_DEFAULT = [
    r"\bmight have\b", r"\bmay have\b", r"\bshould have\b", r"\bcould have\b",
    r"\bI think\b", r"\bI believe\b", r"\bappears? to\b", r"\bseems? to\b",
    r"\btrying to\b", r"\battempting\b", r"\bnot sure\b", r"\bpossibly\b",
    r"\bhopefully\b", r"\broughly\b", r"\bI guess\b", r"\bif (?:it|this)\b",
]

# Evidence a tool RESULT actually produced the asserted outcome. Searched
# in ACTOR tool results (Bash/Edit/Write/NotebookEdit). Includes Edit/Write
# success markers — a file "updated successfully" / "File created
# successfully" IS legitimate backing for a "I created/updated X" claim
# (it's the tool's own result, not a file the agent merely read).
SUCCESS_EVIDENCE_DEFAULT = [
    r"exit code 0\b", r"\b\d+ passed\b", r"all tests? passed", r"Build Succeeded",
    r"BUILD SUCCEEDED", r"Successfully installed", r"Successfully rebased",
    r"-> main\b", r"-> master\b", r"Deployed|deployment (successful|complete)",
    r"\b\d+ files? changed\b", r"✅", r"✓", r"\bfinished\b",
    r"Compiling.*finished",
    # Edit / Write tool success markers.
    r"\bhas been updated successfully\b", r"\bFile created successfully\b",
    r"\bSuccessfully (wrote|wrote \d+|created|updated)\b",
]

# Evidence for FACTUAL claims (existence / state) backed by an
# evidence-producing tool (Grep/Glob/LS/WebFetch/WebSearch). "foo exists" is
# backed by a grep that MATCHED; "the repo uses React" by a grep/cat that
# found it. These patterns detect that the lookup RETURNED A RESULT (a
# match, a file listing, a fetch with content) — distinct from success_evidence
# which detects an actor tool's success marker.
FACT_EVIDENCE_DEFAULT = [
    # grep / rg output: at least one match line (filename:line or N matches).
    r"^\s*\S+:\d+:", r"\b\d+ matches?\b", r"\b\d+ match(?:es)? found\b",
    # Glob / LS: a file listing line (a path with no leading error marker).
    r"^\s*[^\s].*\.\w+\s*$",
    # Web search/fetch returned content (a title / a result count).
    r"\b\d+ results?\b", r"^#\s+\S", r"<title>", r"\[\d+\]",
]

CONFIG_PATH = DATA_DIR / "outcome_rules.json"
_lock = threading.Lock()
_cache: dict | None = None


def _log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _built_in() -> dict:
    return {
        "version": 2,
        # Per-project overrides keyed by cwd basename; "__default__" is the
        # fallback. A project entry may omit any key to inherit the default.
        "projects": {
            "__default__": {
                "outcome_rules": DEFAULT_OUTCOME_RULES,
                "actor_tools": sorted(ACTOR_TOOLS_DEFAULT),
                "evidence_producing_tools": sorted(EVIDENCE_PRODUCING_TOOLS_DEFAULT),
                "non_evidence_tools": sorted(NON_EVIDENCE_TOOLS_DEFAULT),
                "claim_lexicon": CLAIM_LEXICON_DEFAULT,
                "hedge_lexicon": HEDGE_LEXICON_DEFAULT,
                "success_evidence": SUCCESS_EVIDENCE_DEFAULT,
                # Patterns that back a FACTUAL claim (existence / state) from a
                # evidence-producing tool's output — e.g. a grep that MATCHED,
                # an ls that listed files, a git log that emitted commits.
                # These are separate from success_evidence (which backs a
                # "I did X" claim with an actor-tool success marker).
                "fact_evidence": FACT_EVIDENCE_DEFAULT,
            }
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base. Lists are REPLACED (not concatenated) so an
    operator's per-project list fully overrides the default list — that's the
    predictable contract for a rule set. Dicts merge recursively."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_raw() -> dict:
    """Read the JSON config file if present; return {} if absent."""
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"[outcome_rules] config load failed ({exc!r}); using defaults")
    return {}


def load() -> dict:
    """Return the effective config: built-in defaults deep-merged with the
    operator's JSON override (per-project). Cached for a short window so the
    hot-reload still reflects edits without re-reading on every Bash result.
    """
    global _cache
    with _lock:
        # Hot-reload: if the file mtime changed, drop the cache.
        try:
            mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
        except Exception:
            mtime = None
        if _cache is not None and _cache.get("_mtime") == mtime:
            return _cache["cfg"]
        cfg = _deep_merge(_built_in(), _load_raw())
        _cache = {"cfg": cfg, "_mtime": mtime}
        return cfg


def _project_cfg(project: str | None) -> dict:
    cfg = load()
    projects = cfg.get("projects", {}) or {}
    key = (project or "").strip().rstrip("/")
    # Match by full cwd first, then by basename, then default.
    if key and key in projects:
        return _deep_merge(projects["__default__"], projects[key])
    base = key.split("/")[-1] if key else ""
    if base and base in projects:
        return _deep_merge(projects["__default__"], projects[base])
    return projects.get("__default__", {})


def _compile(patterns: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns]


def match_output(project: str | None, tool_name: str, output_text: str) -> str | None:
    """Return the outcome_tag for the first matching rule, or None.

    Matches OUTPUT ONLY (output_text). Never call this with input_json or a
    file's contents — see module docstring for the bug that prevents.
    """
    if not output_text:
        return None
    pcfg = _project_cfg(project)
    rules = pcfg.get("outcome_rules", DEFAULT_OUTCOME_RULES)
    for rule in rules:
        pat = rule.get("pattern")
        if not pat:
            continue
        try:
            if re.search(pat, output_text, re.IGNORECASE | re.MULTILINE):
                return rule.get("tag") or rule.get("outcome")
        except re.error:
            continue
    return None


def evidence_assess(
    project: str | None,
    assistant_text: str,
    tool_results: list[tuple[str, str]],
) -> tuple[bool, bool]:
    """Assess whether assistant_text asserts a success claim and whether a
    same-turn tool RESULT backs it.

    A claim is "backed" if ANY same-turn tool result that is evidence-eligible
    carries matching evidence. Evidence-eligible tools are:
      - ACTOR tools (Bash/Edit/Write/NotebookEdit) — matched against
        success_evidence (exit code 0, "updated successfully", …).
      - EVIDENCE-PRODUCING tools (Grep/Glob/LS/WebFetch/WebSearch) — matched
        against fact_evidence (a grep match, a file listing, a result count).
    NON-evidence tools (bare Read of file contents, TodoWrite, Task*, …) are
    excluded: a success marker inside a file the agent merely READ is not
    evidence the agent produced the outcome (the read-source anti-false-
    positive). The operator can re-enable Read per-project via the config if
    its corpus is clean.

    Returns (claim_present, backed). The caller maps:
      - not claim_present → evidence_backed = NULL (not assessed)
      - claim_present and backed → evidence_backed = 1
      - claim_present and not backed → evidence_backed = 0 (unsupported)
    """
    if not assistant_text:
        return False, False
    pcfg = _project_cfg(project)
    claim_pats = _compile(pcfg.get("claim_lexicon", CLAIM_LEXICON_DEFAULT))
    hedge_pats = _compile(pcfg.get("hedge_lexicon", HEDGE_LEXICON_DEFAULT))
    actor = set(pcfg.get("actor_tools", ACTOR_TOOLS_DEFAULT))
    fact_tools = set(pcfg.get("evidence_producing_tools", EVIDENCE_PRODUCING_TOOLS_DEFAULT))
    non_evidence = set(pcfg.get("non_evidence_tools", NON_EVIDENCE_TOOLS_DEFAULT))
    evidence_pats = _compile(pcfg.get("success_evidence", SUCCESS_EVIDENCE_DEFAULT))
    fact_pats = _compile(pcfg.get("fact_evidence", FACT_EVIDENCE_DEFAULT))

    def _eligible(tool_name: str) -> bool:
        # Explicitly-excluded tools always lose (Read of file contents, etc.).
        if tool_name in non_evidence:
            return False
        return tool_name in actor or tool_name in fact_tools

    claim_present = False
    for cp in claim_pats:
        for m in cp.finditer(assistant_text):
            # Inspect a small window around the claim for hedge words; if
            # the claim is hedged, treat it as a non-claim (don't overfire).
            start = max(0, m.start() - 40)
            end = min(len(assistant_text), m.end() + 40)
            window = assistant_text[start:end]
            if any(hp.search(window) for hp in hedge_pats):
                continue
            claim_present = True
            break
        if claim_present:
            break
    if not claim_present:
        return False, False

    backed = False
    for tool_name, output_text in tool_results:
        if not output_text or not _eligible(tool_name):
            continue
        pats = evidence_pats if tool_name in actor else fact_pats
        if any(p.search(output_text) for p in pats):
            backed = True
            break
    return True, backed


def write_default_config(force: bool = False) -> bool:
    """Seed data/outcome_rules.json with the built-in defaults so the
    operator has a concrete file to edit. Returns True if written."""
    try:
        if CONFIG_PATH.exists() and not force:
            return False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(_built_in(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        _log(f"[outcome_rules] write_default_config failed ({exc!r})")
        return False