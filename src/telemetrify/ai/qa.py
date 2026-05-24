"""Ask-the-Ledger — natural-language Q&A over the corpus.

Two-stage pipeline:
    planner    : question → {semantic_query, filters, intent, k}
    synthesizer: question + retrieved turns → Markdown answer with [#N] citations

Exposed via /ask + /api/ask (SSE streaming) in app.py.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterator

from . import prompts as P, schemas as S
from .client import AnthropicClient, BudgetExceeded
from ..search import Filters, hybrid_search, parse_filters

CITATION_RE = re.compile(r"\[#(\d+)\]")


def plan(conn: sqlite3.Connection, question: str) -> dict:
    """Translate a question to a structured retrieval plan."""
    client = AnthropicClient(conn)
    res = client.call(
        feature="qa",
        template=P.QA_PLANNER,
        user_kwargs={"question": question},
        schema=S.QA_PLANNER,
        max_tokens=400,
        timeout=20.0,
    )
    return res.parsed


def _format_sources(rows: list[dict]) -> str:
    out = []
    for r in rows:
        prompt = (r.get("user_text") or "").strip().replace("\n", " ")[:280]
        resp = (r.get("assistant_text") or "").strip().replace("\n", " ")[:400]
        out.append(
            f"[#{r['id']}] cwd={r.get('cwd') or '—'} model={r.get('model') or '—'} "
            f"date={(r.get('started_at') or '')[:19]}\n"
            f"  PROMPT: {prompt}\n"
            f"  RESPONSE: {resp}"
        )
    return "\n\n".join(out)


def stream_answer(
    conn: sqlite3.Connection, question: str
) -> Iterator[dict]:
    """Run plan → retrieve → synthesize. Yield SSE-friendly events:
        {"event":"plan",    "data":<planner output dict>}
        {"event":"sources", "data":[{id, snippet, ...}]}
        {"event":"delta",   "data":"<markdown chunk>"}   (many)
        {"event":"done",    "data":{"cost_usd":...}}
        {"event":"error",   "data":"<message>"}
    """
    try:
        plan_out = plan(conn, question)
    except BudgetExceeded:
        yield {"event": "error", "data": "daily AI budget exhausted"}
        return
    except Exception as e:
        yield {"event": "error", "data": f"planner failed: {e}"}
        return

    yield {"event": "plan", "data": plan_out}

    semantic = (plan_out.get("semantic_query") or question).strip()
    raw_filters = plan_out.get("filters") or {}
    k = int(plan_out.get("k") or 8)
    filters = parse_filters({str(k_): str(v) for k_, v in raw_filters.items() if v is not None})

    rows = hybrid_search(conn, semantic, k=max(5, min(k, 15)), filters=filters)
    if not rows and filters.where:
        # planner was too restrictive — retry without filters
        rows = hybrid_search(conn, semantic, k=max(5, min(k, 15)), filters=Filters())
    sources = []
    for r in rows:
        sources.append({
            "id": r["id"],
            "session_id": r.get("session_id"),
            "started_at": r.get("started_at"),
            "cwd": r.get("cwd"),
            "model": r.get("model"),
            "prompt_snippet": (r.get("user_text") or "").strip().replace("\n", " ")[:240],
        })
    yield {"event": "sources", "data": sources}

    if not rows:
        yield {"event": "delta", "data": "_No matching turns in the corpus._"}
        yield {"event": "done",  "data": {"cost_usd": 0.0}}
        return

    sources_block = _format_sources(rows)

    # Synthesizer call. schema=None tells the router not to parse / validate
    # the response — the synthesizer returns Markdown, not JSON.
    client = AnthropicClient(conn)
    try:
        res = client.call(
            feature="qa",
            template=P.QA_SYNTHESIZER,
            user_kwargs={"question": question, "k": len(rows),
                         "sources_block": sources_block},
            schema=None,  # free-form Markdown — router skips JSON validation
            max_tokens=1200,
            timeout=45.0,
        )
        answer_text = res.raw_text or ""
    except BudgetExceeded:
        yield {"event": "error", "data": "daily AI budget exhausted (synthesizer)"}
        return

    yield {"event": "delta", "data": answer_text}

    # Roll up cost from this exchange.
    cost_row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS c FROM ai_runs "
        "WHERE feature='qa' AND date(started_at)=date('now')"
    ).fetchone()
    yield {"event": "done", "data": {"cost_usd_today": float(cost_row["c"])}}


def linkify_citations(html_or_md: str) -> str:
    """Replace [#N] tokens with anchor tags pointing at /turns/N."""
    return CITATION_RE.sub(
        lambda m: f'<a class="cite" href="/turns/{m.group(1)}">#{m.group(1)}</a>',
        html_or_md,
    )
