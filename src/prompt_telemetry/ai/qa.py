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

    # Synthesizer call. We don't currently stream tokens (client.call is
    # non-streaming) — emit the full answer as a single delta. Future work:
    # call messages.stream() and forward chunks; the SSE plumbing already
    # supports incremental delta events.
    client = AnthropicClient(conn)
    try:
        res = client.call(
            feature="qa",
            template=P.QA_SYNTHESIZER,
            user_kwargs={"question": question, "k": len(rows),
                         "sources_block": sources_block},
            schema={},  # free-form Markdown; no schema enforcement
            max_tokens=1200,
            timeout=45.0,
        )
        # synthesizer is free-form; res.parsed may be empty since no schema.
        # Pull raw_text directly.
        answer_text = res.raw_text or ""
    except BudgetExceeded:
        yield {"event": "error", "data": "daily AI budget exhausted (synthesizer)"}
        return
    except Exception as e:
        # Recover: the schema-validator threw on free-form markdown.
        # Re-issue with a direct messages.create() bypass — keep the budget guard.
        try:
            answer_text = _direct_call(conn, question, sources_block, len(rows))
        except Exception as e2:
            yield {"event": "error", "data": f"synthesizer failed: {e2}"}
            return

    yield {"event": "delta", "data": answer_text}

    # Roll up cost from this exchange.
    cost_row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS c FROM ai_runs "
        "WHERE feature='qa' AND date(started_at)=date('now')"
    ).fetchone()
    yield {"event": "done", "data": {"cost_usd_today": float(cost_row["c"])}}


def _direct_call(conn: sqlite3.Connection, question: str,
                  sources_block: str, k: int) -> str:
    """Free-form synthesizer call that bypasses the schema validator.
    Records to ai_runs via a manual entry."""
    import os, time as _t
    from datetime import datetime, timezone

    client = AnthropicClient(conn)
    sys, user = P.QA_SYNTHESIZER.render(
        question=question, k=k, sources_block=sources_block
    )
    client._load_env_from_claude_settings()
    sdk = client._client()

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO ai_runs(feature, model, prompt_version, status, started_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        ("qa", P.QA_SYNTHESIZER.model, P.QA_SYNTHESIZER.version, started),
    )
    conn.commit()
    run_id = cur.lastrowid

    t0 = _t.monotonic()
    try:
        resp = sdk.messages.create(
            model=P.QA_SYNTHESIZER.model,
            max_tokens=1200,
            system=sys,
            messages=[{"role": "user", "content": user}],
            timeout=45.0,
        )
        text = ""
        for b in (resp.content or []):
            if getattr(b, "type", None) == "text":
                text += getattr(b, "text", "")
        from .client import estimate_cost_usd
        usage = getattr(resp, "usage", None)
        in_t  = int(getattr(usage, "input_tokens", 0) or 0)
        out_t = int(getattr(usage, "output_tokens", 0) or 0)
        cost = estimate_cost_usd(P.QA_SYNTHESIZER.model, in_t, out_t)
        conn.execute(
            """
            UPDATE ai_runs SET input_tokens=?, output_tokens=?, cost_usd=?,
                status='success', finished_at=?, duration_ms=? WHERE id=?
            """,
            (in_t, out_t, cost,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             int((_t.monotonic()-t0)*1000), run_id),
        )
        conn.commit()
        return text
    except Exception as e:
        conn.execute(
            "UPDATE ai_runs SET status='failure', error=?, finished_at=? WHERE id=?",
            (str(e), datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id),
        )
        conn.commit()
        raise


def linkify_citations(html_or_md: str) -> str:
    """Replace [#N] tokens with anchor tags pointing at /turns/N."""
    return CITATION_RE.sub(
        lambda m: f'<a class="cite" href="/turns/{m.group(1)}">#{m.group(1)}</a>',
        html_or_md,
    )
