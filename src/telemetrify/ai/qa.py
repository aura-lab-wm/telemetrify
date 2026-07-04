"""Ask-the-Ledger — natural-language Q&A over the corpus.

Two-stage pipeline:
    planner    : question → {semantic_query, filters, intent, k}
    synthesizer: question + retrieved turns → Markdown answer with [#N] citations

Exposed via /ask + /api/ask (SSE streaming) in app.py.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Iterator

from . import prompts as P, schemas as S
from .client import AnthropicClient, BudgetExceeded
from ..search import Filters, hybrid_search, parse_filters

CITATION_RE = re.compile(r"\[#(\d+)\]")

# Hard wall-clock bound on retrieval (embedding + hybrid_search). The embed
# model is lazily loaded on first use; if that load (or the search itself)
# ever wedges, this keeps the request from hanging forever and permanently
# leaking an anyio thread-pool slot — it fails cleanly with an SSE error
# event instead. 40s is generous for a cold model load + query.
RETRIEVAL_TIMEOUT_S = 40.0

# The PLANNER must return a strict JSON object. The Mac-local Ollama tier
# returns prose (→ "AI response not JSON"), so the planner pins a tier order
# that prefers the reliable-JSON claude_cli tier. Rocco stays first (free 72B
# when the GPU box is up); anthropic is the last resort. The SYNTHESIZER does
# NOT use this — it returns free-form Markdown, so it's happy on the faster
# default order (which keeps the cheap local tier). Override via env if needed.
_PLANNER_ORDER_DEFAULT = ("rocco", "claude_cli", "anthropic")


def _planner_order() -> list[str]:
    env = os.environ.get("TELEMETRIFY_LLM_ORDER__qa_planner")
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return list(_PLANNER_ORDER_DEFAULT)


def plan(conn: sqlite3.Connection, question: str, *,
         model_override: str | None = None, feature: str = "qa") -> dict:
    """Translate a question to a structured retrieval plan."""
    client = AnthropicClient(conn)
    res = client.call(
        feature=feature,
        template=P.QA_PLANNER,
        user_kwargs={"question": question},
        schema=S.QA_PLANNER,
        max_tokens=400,
        timeout=20.0,
        order=_planner_order(),
        model_override=model_override,
    )
    return res.parsed


def _retrieve_with_timeout(
    conn: sqlite3.Connection, semantic: str, *, k: int, filters: Filters,
    timeout: float = RETRIEVAL_TIMEOUT_S,
) -> list[dict]:
    """Run hybrid_search (retrying without filters if a filtered search comes
    back empty) with a hard wall-clock timeout.

    hybrid_search() calls embed(q), which lazily loads the sentence-transformers
    model on first use (see embed.py's _model()). Before that loader was made
    thread-safe, concurrent first-callers could race on construction; even with
    that fixed, this is a defense-in-depth bound so a genuinely stuck embed/
    search call can never hang the request (and its SSE generator) forever.

    The call runs in a dedicated worker thread so `.result(timeout=...)` can
    give up on waiting. `conn` is bound to whichever thread is currently
    driving this request's SSE generator (Starlette's `iterate_in_threadpool`
    can resume a sync generator on a *different* worker thread on each
    `next()` call), so it is not safe to reuse from a brand-new thread under
    sqlite3's default `check_same_thread=True`. The worker instead opens its
    own short-lived connection to the same database file (same approach as
    `db.connect_uncached`, minus the redundant migrations check — the
    passed-in `conn` has already been migrated) and closes it when done.

    NOTE: if the call genuinely hangs, `.result(timeout=...)` still returns
    (with TimeoutError) — it just stops waiting. The orphaned worker thread
    is abandoned to run to completion (or hang) on its own, off the shared
    anyio thread pool, rather than permanently consuming one of its slots.
    """
    try:
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"] or None
    except Exception:
        db_path = None

    def _run() -> list[dict]:
        if db_path is None:
            # No on-disk file (e.g. an in-memory DB) — can't open a second
            # connection to it, so fall back to the caller's connection.
            local_conn, close = conn, False
        else:
            from ..db import _raw_connect
            local_conn, close = _raw_connect(Path(db_path), check_same_thread=False), True
        try:
            rows = hybrid_search(local_conn, semantic, k=k, filters=filters)
            if not rows and filters.where:
                # planner was too restrictive — retry without filters
                rows = hybrid_search(local_conn, semantic, k=k, filters=Filters())
            return rows
        finally:
            if close:
                local_conn.close()

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qa-retrieval")
    future = executor.submit(_run)
    try:
        return future.result(timeout=timeout)
    finally:
        # wait=False: never block on a possibly-hung worker. If _run() is
        # genuinely stuck, this abandons the thread instead of stalling here.
        executor.shutdown(wait=False)


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
    conn: sqlite3.Connection, question: str, *,
    model_override: str | None = None, feature: str = "qa",
) -> Iterator[dict]:
    """Run plan → retrieve → synthesize. Yield SSE-friendly events:
        {"event":"plan",    "data":<planner output dict>}
        {"event":"sources", "data":[{id, snippet, ...}]}
        {"event":"delta",   "data":"<markdown chunk>"}   (many)
        {"event":"done",    "data":{"cost_usd":...}}
        {"event":"error",   "data":"<message>"}
    """
    try:
        plan_out = plan(conn, question, model_override=model_override, feature=feature)
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
    eff_k = max(5, min(k, 15))

    try:
        # Pass the module constant explicitly (rather than relying on
        # _retrieve_with_timeout's default, which is bound once at import
        # time) so tests — and any future runtime override — can adjust
        # RETRIEVAL_TIMEOUT_S and have it actually take effect here.
        rows = _retrieve_with_timeout(conn, semantic, k=eff_k, filters=filters,
                                       timeout=RETRIEVAL_TIMEOUT_S)
    except FutureTimeoutError:
        yield {"event": "error",
               "data": f"retrieval timed out after {RETRIEVAL_TIMEOUT_S:.0f}s "
                       "(embedding/search took too long)"}
        return
    except Exception as e:
        yield {"event": "error", "data": f"retrieval failed: {e}"}
        return
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
            feature=feature,
            template=P.QA_SYNTHESIZER,
            user_kwargs={"question": question, "k": len(rows),
                         "sources_block": sources_block},
            schema=None,  # free-form Markdown — router skips JSON validation
            max_tokens=1200,
            timeout=45.0,
            model_override=model_override,
        )
        answer_text = res.raw_text or ""
    except BudgetExceeded:
        yield {"event": "error", "data": "daily AI budget exhausted (synthesizer)"}
        return
    except Exception as e:
        # Align with the planner's handling above: any other failure (rate
        # limit, all-tiers-down, network error, ...) must degrade to a clean
        # SSE error event, not propagate and crash the ASGI app.
        yield {"event": "error", "data": f"synthesizer failed: {e}"}
        return

    yield {"event": "delta", "data": answer_text}

    # Roll up cost from this exchange.
    cost_row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS c FROM ai_runs "
        "WHERE feature=? AND date(started_at)=date('now')",
        (feature,),
    ).fetchone()
    yield {"event": "done", "data": {"cost_usd_today": float(cost_row["c"])}}


def linkify_citations(html_or_md: str) -> str:
    """Replace [#N] tokens with anchor tags pointing at /turns/N."""
    return CITATION_RE.sub(
        lambda m: f'<a class="cite" href="/turns/{m.group(1)}">#{m.group(1)}</a>',
        html_or_md,
    )
