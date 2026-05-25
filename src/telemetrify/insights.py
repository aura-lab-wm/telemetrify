"""Insights — pre-computed answers to a curated set of self-reflection
questions about the user's own prompting behavior.

The point: telemetrify holds 12,798 turns of prompt history; the user
shouldn't have to think "what to ask" every time they want introspection.
A curated library of standardized questions, pre-computed nightly,
turns the corpus into a glanceable mirror.

This module owns:

  - CURATED_QUESTIONS: the single source of truth. The same list drives
    the suggestion chips on /ask AND the cards on /insights, so adding
    a question is a one-place change.

  - compute_all(): runs each question through the same /api/ask
    pipeline (plan → retrieve → synthesize), persists results to
    data/insights.json. Skips entries whose cache is fresh (≤ TTL).

  - load_cached(): the route's read path; returns {} when no cache.

Cost: ~13 questions × $0.001 (Haiku via Anthropic) ≈ $0.013 per run.
Routed to local Rocco/Kimi when up → effectively free.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# Cache file lives next to the SQLite DB so backups + .gitignore already
# cover it. Re-writes are atomic via a sibling tmp + os.replace.
INSIGHTS_PATH = Path(__file__).resolve().parents[2] / "data" / "insights.json"
DEFAULT_FRESHNESS_S = 23 * 3600  # re-run if older than ~23h


@dataclass(frozen=True)
class Question:
    """A curated self-reflection question.

    `id` is stable across versions — used as the cache key. Never
    rename an existing id; deprecate + add a new one. `prompt` is what
    the planner sees verbatim; keep it phrased as a full sentence so
    the LLM understands the structural hint (e.g. "ZERO follow-up
    corrections" / "by cwd" / "the next turn was 'no, what I meant
    was'").
    """
    id: str
    category: str  # "friction" | "weak prompts" | "trends" | "best moments" | "working memory"
    label: str     # chip text on /ask + card title on /insights
    prompt: str    # the actual question fed to /api/ask


CURATED_QUESTIONS: list[Question] = [
    # ── FRICTION ─────────────────────────────────────────────────────────
    Question("friction.reasked",
             "friction", "prompts I re-asked most",
             "Find the 10 prompts I had to re-ask the most times in the "
             "same session before getting a useful answer, and group them "
             "by what topic they had in common."),
    Question("friction.longest_chains",
             "friction", "longest correction chains",
             "Which of my sessions had the longest chain of follow-up "
             "corrections, and what was the root issue I was struggling "
             "to convey?"),
    Question("friction.underspecified",
             "friction", "underspecified prompts",
             "Show me cases where I clearly didn't give Claude enough "
             "context up-front — find prompts where my next turn was "
             "'no, what I meant was…' or similar."),

    # ── WEAK PROMPTS ─────────────────────────────────────────────────────
    Question("weak.vague_oneliners",
             "weak prompts", "vague one-liners",
             "Find my one-line vague prompts ('do it', 'fix this', "
             "'now what') that triggered the longest assistant responses "
             "— those are signals that I made Claude guess and it tried "
             "to over-cover."),
    Question("weak.tool_errors",
             "weak prompts", "prompts that caused tool errors",
             "Which prompts triggered tool errors that I then had to "
             "re-prompt to fix? Tell me the pattern — am I asking for "
             "the wrong tool, missing args, or assuming state that "
             "didn't exist?"),
    Question("weak.needed_clarification",
             "weak prompts", "prompts that needed clarification",
             "Show me prompts where the assistant asked me a clarifying "
             "question back — those are cases where I should have "
             "included the answer up-front. What do those questions have "
             "in common?"),

    # ── TRENDS ──────────────────────────────────────────────────────────
    Question("trends.topic_drift",
             "trends", "topic drift over time",
             "What topics am I asking about most this week vs the "
             "previous four weeks? Where is my attention shifting?"),
    Question("trends.projects_spend",
             "trends", "projects by prompt-spend",
             "Which projects (by cwd) consume the most of my prompt "
             "time, and what kinds of tasks dominate each one?"),
    Question("trends.time_of_day",
             "trends", "when am I sharpest",
             "What time of day do I prompt most, and is the quality "
             "(measured by no-follow-ups + no-tool-errors) different "
             "across morning / afternoon / late-night?"),

    # ── BEST MOMENTS ────────────────────────────────────────────────────
    Question("best.best_ever",
             "best moments", "my best prompts ever",
             "Find the 10 longest sessions that had ZERO follow-up "
             "corrections — what made those prompts work? Quote the "
             "opener verbatim."),
    Question("best.good_annotation",
             "best moments", "prompts that earned a good annotation",
             "Which prompts produced answers I annotated positively, "
             "and what stylistic patterns do those prompts share?"),

    # ── WORKING MEMORY ──────────────────────────────────────────────────
    Question("memory.yesterday",
             "working memory", "yesterday's work",
             "What did I work on yesterday? Group by project, list the "
             "open threads I might want to pick up."),
    Question("memory.resume_project",
             "working memory", "resume current project",
             "What was I in the middle of the last time I touched THIS "
             "project (look at my cwd)? Last 5 sessions in this dir, "
             "summarized."),
    Question("memory.loose_ends",
             "working memory", "loose ends",
             "Which unfinished problems from the last two weeks did I "
             "never circle back to?"),
]


def categories() -> list[str]:
    """Stable order in which categories should be rendered."""
    seen: list[str] = []
    for q in CURATED_QUESTIONS:
        if q.category not in seen:
            seen.append(q.category)
    return seen


def questions_for_chips() -> list[dict[str, str]]:
    """Flat list the /ask Jinja template iterates over (id, category,
    label, prompt). Keeps the template dumb."""
    return [asdict(q) for q in CURATED_QUESTIONS]


# ─── Cache layer ──────────────────────────────────────────────────────────


def load_cached() -> dict[str, Any]:
    """Return whatever's in data/insights.json — empty dict if missing.

    Shape:
      {
        "computed_at": int (unix ts of LATEST entry written),
        "entries": {
          "<question_id>": {
            "question": str, "label": str, "category": str,
            "answer_md": str, "sources": [...], "model_used": str,
            "computed_at": int, "duration_ms": int
          }
        }
      }
    """
    if not INSIGHTS_PATH.exists():
        return {"computed_at": 0, "entries": {}}
    try:
        return json.loads(INSIGHTS_PATH.read_text())
    except Exception:
        return {"computed_at": 0, "entries": {}}


def _atomic_write(payload: dict[str, Any]) -> None:
    INSIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INSIGHTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(INSIGHTS_PATH)


# ─── Compute path ────────────────────────────────────────────────────────


def is_fresh(entry: dict[str, Any], ttl_s: int = DEFAULT_FRESHNESS_S) -> bool:
    return (time.time() - int(entry.get("computed_at", 0))) < ttl_s


def compute_one(conn, question: Question) -> dict[str, Any]:
    """Run a single question through the /api/ask SSE pipeline and
    collect the final answer + sources. Returns the entry shape that
    load_cached() expects.

    Pin both planner AND synthesizer to Haiku for this batch. Sonnet
    is the default synth model and burns the shared OAuth bucket fast
    — when Sonnet 429s, the router falls through to localmac
    (Ollama on the Mac), which spins fans. Haiku is plenty for
    reflection-style "summarize these 8 retrieved turns" prompts.
    Saves the Mac AND keeps OAuth budget for /ask interactive use.
    """
    from .ai import prompts as P
    saved_planner = P.QA_PLANNER.model
    saved_synth = P.QA_SYNTHESIZER.model
    # PromptTemplate is a frozen dataclass; mutate __dict__ directly.
    object.__setattr__(P.QA_PLANNER,     "model", "claude-haiku-4-5-20251001")
    object.__setattr__(P.QA_SYNTHESIZER, "model", "claude-haiku-4-5-20251001")

    from .ai.qa import stream_answer

    t0 = time.monotonic()
    answer_md = ""
    sources: list[dict[str, Any]] = []
    model_used = ""
    error: str | None = None
    try:
        events = list(stream_answer(conn, question.prompt))
    finally:
        object.__setattr__(P.QA_PLANNER,     "model", saved_planner)
        object.__setattr__(P.QA_SYNTHESIZER, "model", saved_synth)

    for evt in events:
        if evt["event"] == "sources":
            sources = evt["data"]
        elif evt["event"] == "delta":
            answer_md += evt["data"]
        elif evt["event"] == "error":
            error = evt["data"]
            break
        elif evt["event"] == "done":
            # The synthesizer doesn't expose the model id on `done`;
            # the cost is captured but the model id isn't propagated
            # by stream_answer today. Leave model_used empty until
            # qa.py grows that field.
            pass

    return {
        "id": question.id,
        "category": question.category,
        "label": question.label,
        "question": question.prompt,
        "answer_md": answer_md,
        "sources": sources,
        "model_used": model_used,
        "error": error,
        "computed_at": int(time.time()),
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def compute_all(conn, force_refresh: bool = False,
                ttl_s: int = DEFAULT_FRESHNESS_S,
                progress=print) -> dict[str, Any]:
    """Compute every CURATED_QUESTION whose cached entry is stale (or
    missing). `progress` is a callable receiving log lines — bin/insights
    passes `print`, the FastAPI route passes a no-op.

    Backend preference is pinned for this batch operation:
      rocco → anthropic → ollama → localmac
    localmac is DEAD LAST on purpose — running 14 × (planner+synth)
    through gpt-oss:20b on the user's Mac heats the M-series chip
    enough to spin the fans audibly. Anthropic Haiku is ~$0.001/call
    and finishes silently. Honor an explicit user override (env var
    already set) if present.
    """
    import os
    if not os.environ.get("TELEMETRIFY_LLM_ORDER__qa"):
        os.environ["TELEMETRIFY_LLM_ORDER__qa"] = (
            "rocco,anthropic,ollama,localmac"
        )
        progress(f"[insights] backend order pinned: "
                 f"{os.environ['TELEMETRIFY_LLM_ORDER__qa']}")
    cached = load_cached()
    entries: dict[str, Any] = dict(cached.get("entries") or {})
    for q in CURATED_QUESTIONS:
        existing = entries.get(q.id)
        if (existing and not force_refresh
                and is_fresh(existing, ttl_s=ttl_s)):
            progress(f"[insights] skip {q.id} (fresh)")
            continue
        progress(f"[insights] compute {q.id} — {q.label!r}")
        try:
            entries[q.id] = compute_one(conn, q)
            if entries[q.id].get("error"):
                progress(f"[insights]   error: {entries[q.id]['error']}")
            else:
                progress(f"[insights]   ok ({entries[q.id]['duration_ms']}ms)")
        except Exception as e:  # noqa: BLE001 — keep going
            progress(f"[insights]   FAILED {q.id}: {e}")
            entries[q.id] = {
                "id": q.id, "category": q.category, "label": q.label,
                "question": q.prompt, "answer_md": "",
                "sources": [], "model_used": "", "error": str(e),
                "computed_at": int(time.time()), "duration_ms": 0,
            }

    payload = {
        "computed_at": int(time.time()),
        "entries": entries,
    }
    _atomic_write(payload)
    progress(f"[insights] wrote {INSIGHTS_PATH}")
    return payload
