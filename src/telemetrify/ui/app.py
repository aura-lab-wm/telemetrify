from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

from ..db import connect, serialize_embedding
from ..embed import embed
from ..search import hybrid_search, parse_filters, recent, similar_turns, ALLOWED_FILTERS
from ..doctor import run_health_checks
from ..charts import CHARTS
from ..export import export_jsonl, export_csv
from ..rerun import run_rerun, render_inline_diff, DEFAULT_BUDGET_USD
from ..dash0.receiver import router as dash0_router

# ─── CSRF mitigation for state-changing routes ─────────────────────────
#
# This app has no auth/session system at all (single-operator, localhost-
# bound tool) so a full CSRF-token scheme would be over-engineering. The
# real threat here is a *different* origin — some other page the operator
# has open in the same browser — issuing a cross-origin fetch/form POST
# against this service (which is reachable at a fixed, predictable
# localhost URL). Browsers attach `Origin` (and, failing that, `Referer`)
# on cross-origin state-changing requests, so requiring an exact match
# against this app's own origin blocks that vector while imposing zero
# friction on the dashboard's own same-origin JS/forms.
#
# Requests carrying neither header (curl, the CLI, TestClient, other local
# tooling) are allowed through: they cannot be mounted by a hostile web
# page, which is the only thing this guards against.
SAME_ORIGIN_ALLOWLIST = {"http://localhost:8767", "http://127.0.0.1:8767"}


def require_same_origin(request: Request) -> None:
    """FastAPI dependency: 403s a state-changing request whose Origin/Referer
    names a different origin than this app's own. See module comment above."""
    candidate = request.headers.get("origin")
    if candidate is None:
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            if parts.scheme and parts.netloc:
                candidate = f"{parts.scheme}://{parts.netloc}"
            else:
                candidate = referer
    if candidate is not None and candidate not in SAME_ORIGIN_ALLOWLIST:
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

_env = Environment(
    loader=FileSystemLoader(str(HERE / "templates")),
    autoescape=select_autoescape(["html"]),
)

app = FastAPI(title="Prompt Telemetry")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(dash0_router)

# html=False: the commonmark preset enables raw-HTML passthrough, but the
# rendered output is injected with `|md|safe` into templates over
# attacker-influenced captured text (prompts/responses). Disabling raw HTML
# escapes <script>/<img onerror> while keeping markdown formatting; markdown-it's
# default validateLink already blocks javascript:/data: URLs.
_md = MarkdownIt("commonmark", {"breaks": True, "linkify": True, "html": False})


def render_md(text: str) -> str:
    return _md.render(text or "")


def _snippet(text: str, limit: int = 280) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact(n) -> str:
    """Abbreviate large counts so the stats bar stays glanceable:
    291999084 → '292M', 103990 → '104K', 13493 → '13.5K', 842 → '842'."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    a = abs(n)
    if a < 1000:
        return str(n)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if a >= div:
            v = n / div
            return f"{v:.0f}{suf}" if abs(v) >= 100 else f"{v:.1f}{suf}".replace(".0", "")
    return str(n)


def _commas(n) -> str:
    """Exact value with thousands separators — used for hover titles."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


_env.filters["md"] = render_md
_env.filters["snippet"] = _snippet
_env.filters["compact"] = _compact
_env.filters["commas"] = _commas


def _render(name: str, ctx: dict) -> HTMLResponse:
    return HTMLResponse(_env.get_template(name).render(**ctx))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = connect()
    params = dict(request.query_params)
    q = (params.get("q") or "").strip()
    try:
        k = max(1, min(int(params.get("k", "20")), 100))
    except ValueError:
        k = 20

    filters = parse_filters(params)
    active_filters = {key: params[key] for key in ALLOWED_FILTERS if params.get(key)}

    sessions = conn.execute(
        """
        SELECT s.id, s.started_at, s.cwd, COUNT(t.id) AS turn_count,
               MAX(t.started_at) AS last_turn_at
        FROM sessions s
        LEFT JOIN turns t ON t.session_id = s.id
        GROUP BY s.id
        ORDER BY COALESCE(MAX(t.started_at), s.started_at) DESC
        LIMIT 50
        """
    ).fetchall()

    stats = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM sessions) AS sessions,
          (SELECT COUNT(*) FROM turns)    AS turns,
          (SELECT COUNT(*) FROM tool_calls) AS tool_calls,
          (SELECT COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) FROM turns) AS tokens
        """
    ).fetchone()

    if q:
        results = hybrid_search(conn, q, k=k, filters=filters)
        mode = "search"
    else:
        results = recent(conn, k=k, filters=filters)
        mode = "recent"

    # Distinct values for filter dropdowns.
    facets = {
        "models": [r["model"] for r in conn.execute(
            "SELECT DISTINCT model FROM turns WHERE model IS NOT NULL ORDER BY model"
        ).fetchall()],
        "skills": [r["attribution_skill"] for r in conn.execute(
            "SELECT DISTINCT attribution_skill FROM turns WHERE attribution_skill IS NOT NULL ORDER BY attribution_skill"
        ).fetchall()],
        "origins": [r["origin"] for r in conn.execute(
            "SELECT DISTINCT origin FROM turns ORDER BY origin"
        ).fetchall()],
        # Distinct individual annotation tags (the `tags` column is CSV).
        "tags": sorted({
            t.strip()
            for (csv,) in conn.execute(
                "SELECT tags FROM annotations WHERE tags IS NOT NULL AND tags <> ''"
            ).fetchall()
            for t in csv.split(",")
            if t.strip()
        }),
    }

    return _render("index.html", {
        "q": q,
        "k": k,
        "results": results,
        "sessions": [dict(s) for s in sessions],
        "mode": mode,
        "stats": dict(stats) if stats else {},
        "active_filters": active_filters,
        "facets": facets,
    })


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(request: Request, session_id: str):
    conn = connect()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        return HTMLResponse("session not found", status_code=404)
    turns = conn.execute(
        "SELECT * FROM turns WHERE session_id = ? ORDER BY started_at ASC",
        (session_id,),
    ).fetchall()
    return _render("session.html", {
        "session": dict(session),
        "turns": [dict(t) for t in turns],
        "q": "",
    })


@app.get("/turns/{turn_id}", response_class=HTMLResponse)
def turn_detail(request: Request, turn_id: int):
    conn = connect()
    turn = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if not turn:
        return HTMLResponse("turn not found", status_code=404)
    turn = dict(turn)
    turn.pop("raw_json_z", None)  # keep BLOB out of template ctx
    tool_calls = conn.execute(
        "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY seq ASC", (turn_id,)
    ).fetchall()
    siblings = conn.execute(
        "SELECT id FROM turns WHERE session_id = ? ORDER BY started_at ASC",
        (turn["session_id"],),
    ).fetchall()
    ids = [s["id"] for s in siblings]
    idx = ids.index(turn["id"])
    annotation = conn.execute("SELECT * FROM annotations WHERE turn_id = ?", (turn_id,)).fetchone()
    cluster_row = conn.execute(
        """
        SELECT pc.id, pc.label, pc.member_count, tc.similarity_to_centroid
        FROM turn_cluster tc JOIN prompt_clusters pc ON pc.id = tc.cluster_id
        WHERE tc.turn_id = ?
        """,
        (turn_id,),
    ).fetchone()
    followup = conn.execute(
        "SELECT * FROM turn_followups WHERE turn_id = ?", (turn_id,)
    ).fetchone()
    similar = similar_turns(conn, turn_id, k=5)
    return _render("turn.html", {
        "turn": turn,
        "tool_calls": [dict(t) for t in tool_calls],
        "prev_id": ids[idx - 1] if idx > 0 else None,
        "next_id": ids[idx + 1] if idx < len(ids) - 1 else None,
        "annotation": dict(annotation) if annotation else None,
        "cluster": dict(cluster_row) if cluster_row else None,
        "followup": dict(followup) if followup else None,
        "similar": similar,
        "q": "",
    })


@app.post("/api/annotations/{turn_id}")
def upsert_annotation(
    turn_id: int,
    rating: int = Form(0),
    label: str = Form(""),
    tags: str = Form(""),
    expected_behavior: str = Form(""),
    notes: str = Form(""),
    _origin: None = Depends(require_same_origin),
):
    conn = connect()
    exists = conn.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if not exists:
        return JSONResponse({"error": "turn not found"}, status_code=404)
    rating = max(-1, min(1, int(rating)))
    with conn:
        conn.execute(
            """
            INSERT INTO annotations(turn_id, rating, label, tags, expected_behavior, notes,
                                    created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(turn_id) DO UPDATE SET
                rating            = excluded.rating,
                label             = excluded.label,
                tags              = excluded.tags,
                expected_behavior = excluded.expected_behavior,
                notes             = excluded.notes,
                updated_at        = excluded.updated_at
            """,
            (turn_id, rating, label.strip() or None, tags.strip() or None,
             expected_behavior.strip() or None, notes.strip() or None),
        )
    return RedirectResponse(f"/turns/{turn_id}", status_code=303)


@app.post("/api/annotations/{turn_id}/delete")
def delete_annotation(turn_id: int, _origin: None = Depends(require_same_origin)):
    conn = connect()
    with conn:
        conn.execute("DELETE FROM annotations WHERE turn_id = ?", (turn_id,))
    return RedirectResponse(f"/turns/{turn_id}", status_code=303)


@app.get("/clusters", response_class=HTMLResponse)
def cluster_index(request: Request):
    conn = connect()
    rows = conn.execute(
        """
        SELECT pc.id, pc.label, pc.auto_label, pc.member_count,
               pc.representative_turn_id, pc.updated_at,
               (SELECT MAX(t.started_at) FROM turn_cluster tc JOIN turns t ON t.id = tc.turn_id
                 WHERE tc.cluster_id = pc.id) AS last_seen
        FROM prompt_clusters pc
        ORDER BY pc.member_count DESC, pc.updated_at DESC
        """
    ).fetchall()
    return _render("clusters.html", {"clusters": [dict(r) for r in rows], "q": ""})


@app.get("/clusters/{cluster_id}", response_class=HTMLResponse)
def cluster_detail(request: Request, cluster_id: int):
    conn = connect()
    cluster = conn.execute("SELECT * FROM prompt_clusters WHERE id = ?", (cluster_id,)).fetchone()
    if not cluster:
        return HTMLResponse("cluster not found", status_code=404)
    members = conn.execute(
        """
        SELECT t.id, t.user_text, t.assistant_text, t.session_id, t.started_at, t.cwd,
               t.model, t.origin, tc.similarity_to_centroid
        FROM turn_cluster tc JOIN turns t ON t.id = tc.turn_id
        WHERE tc.cluster_id = ?
        ORDER BY t.started_at DESC
        """,
        (cluster_id,),
    ).fetchall()
    return _render("cluster.html", {
        "cluster": dict(cluster),
        "members": [dict(m) for m in members],
        "q": "",
    })


@app.get("/api/turns/{turn_id}", response_class=JSONResponse)
def turn_json(turn_id: int):
    conn = connect()
    turn = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if not turn:
        return JSONResponse({"error": "not found"}, status_code=404)
    tool_calls = conn.execute(
        "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY seq ASC", (turn_id,)
    ).fetchall()
    out = dict(turn)
    raw_z = out.pop("raw_json_z", None)
    if raw_z:
        try:
            from ..raw_archive import decompress_json
            out["raw_json"] = decompress_json(raw_z)
        except Exception:
            out["raw_json"] = None
    return JSONResponse({
        "turn": out,
        "tool_calls": [dict(t) for t in tool_calls],
    })


@app.get("/api/stats", response_class=JSONResponse)
def stats():
    conn = connect()
    out = {}
    out["sessions"] = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
    out["turns"]    = conn.execute("SELECT COUNT(*) AS c FROM turns").fetchone()["c"]
    out["tool_calls"] = conn.execute("SELECT COUNT(*) AS c FROM tool_calls").fetchone()["c"]
    out["tool_breakdown"] = [
        dict(r) for r in conn.execute(
            "SELECT tool_name, COUNT(*) AS n FROM tool_calls GROUP BY tool_name ORDER BY n DESC"
        ).fetchall()
    ]
    out["model_breakdown"] = [
        dict(r) for r in conn.execute(
            "SELECT model, COUNT(*) AS n FROM turns GROUP BY model ORDER BY n DESC"
        ).fetchall()
    ]
    return JSONResponse(out)


@app.get("/api/health", response_class=JSONResponse)
def health():
    return JSONResponse(run_health_checks())


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    return _render("dashboard.html", {"q": ""})


@app.get("/start", response_class=HTMLResponse)
def start(request: Request):
    """Newbie landing: 30-second tour + per-section explainers + search syntax.

    Detects an empty DB (no turns) and swaps in a 'wire your Stop hook'
    first-run block instead of the tour.
    """
    c = connect()
    r = c.execute("SELECT COUNT(*) AS n FROM turns").fetchone()
    turns_total = int(r["n"] or 0)
    today_iso = c.execute("SELECT date('now') AS d").fetchone()["d"]
    return _render(
        "start.html",
        {"q": "", "turns_total": turns_total, "today": today_iso, "request": request},
    )


@app.get("/api/dashboard/headline", response_class=JSONResponse)
def dashboard_headline():
    """Narrative-fold data: sessions, turns, first/last entry, follow-up rate,
    cache-hit ratio, models seen, top tool. Used by the ledger header."""
    c = connect()
    r = c.execute("""
        SELECT COUNT(*) AS sessions FROM sessions
    """).fetchone()
    sessions = r["sessions"]
    r = c.execute("""
        SELECT COUNT(*) AS turns, MIN(started_at) AS first_at, MAX(started_at) AS last_at,
               SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) AS tokens,
               SUM(COALESCE(cache_read_tokens,0)) AS cache_hit,
               SUM(COALESCE(cache_creation_tokens,0)) AS cache_miss
        FROM turns
    """).fetchone()
    followups = c.execute("SELECT COUNT(*) AS n FROM turn_followups").fetchone()["n"]
    clusters = c.execute("SELECT COUNT(*) AS n FROM prompt_clusters").fetchone()["n"]
    annotations = c.execute("SELECT COUNT(*) AS n FROM annotations").fetchone()["n"]
    top_tool = c.execute("""
        SELECT tool_name, COUNT(*) AS n FROM tool_calls
        GROUP BY tool_name ORDER BY n DESC LIMIT 1
    """).fetchone()
    top_model = c.execute("""
        SELECT model, COUNT(*) AS n FROM turns
        WHERE model IS NOT NULL GROUP BY model ORDER BY n DESC LIMIT 1
    """).fetchone()
    turns = r["turns"] or 0
    cache_hit = int(r["cache_hit"] or 0)
    cache_miss = int(r["cache_miss"] or 0)
    return JSONResponse({
        "sessions": sessions,
        "turns": turns,
        "first_at": r["first_at"],
        "last_at": r["last_at"],
        "tokens": int(r["tokens"] or 0),
        "followups": followups,
        "followup_pct": (100.0 * followups / turns) if turns else 0.0,
        "clusters": clusters,
        "annotations": annotations,
        "cache_hit_pct": (100.0 * cache_hit / (cache_hit + cache_miss)) if (cache_hit + cache_miss) else 0.0,
        "top_tool": dict(top_tool) if top_tool else None,
        "top_model": dict(top_model) if top_model else None,
    })


@app.get("/api/pulse", response_class=JSONResponse)
def api_pulse():
    """Live-now blob feeding the home Pulse card and the topbar chip.

    Single round-trip: last-hour totals, last-24h hourly sparkline, the
    five most recent turns, and the gap since the last turn. Cheap enough
    to poll every 30 s.
    """
    c = connect()
    now_row = c.execute("SELECT datetime('now') AS now").fetchone()
    server_now = now_row["now"]

    last = c.execute(
        "SELECT MAX(started_at) AS last_at FROM turns"
    ).fetchone()
    last_at = last["last_at"]
    minutes_since = None
    if last_at:
        delta = c.execute(
            "SELECT (julianday('now') - julianday(?)) * 1440.0 AS m",
            (last_at,),
        ).fetchone()
        minutes_since = float(delta["m"] or 0.0)

    hr = c.execute(
        """
        SELECT COUNT(*) AS turns,
               SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) AS tokens
        FROM turns
        WHERE started_at >= datetime('now', '-60 minutes')
        """
    ).fetchone()
    hr_errors = c.execute(
        """
        SELECT COUNT(DISTINCT tc.turn_id) AS n
        FROM tool_calls tc
        JOIN turns t ON t.id = tc.turn_id
        WHERE tc.is_error = 1
          AND t.started_at >= datetime('now', '-60 minutes')
        """
    ).fetchone()
    hr_top_model = c.execute(
        """
        SELECT model, COUNT(*) AS n FROM turns
        WHERE started_at >= datetime('now', '-60 minutes')
          AND model IS NOT NULL
        GROUP BY model ORDER BY n DESC LIMIT 1
        """
    ).fetchone()
    hr_top_cwd = c.execute(
        """
        SELECT cwd, COUNT(*) AS n FROM turns
        WHERE started_at >= datetime('now', '-60 minutes')
          AND cwd IS NOT NULL AND cwd != ''
        GROUP BY cwd ORDER BY n DESC LIMIT 1
        """
    ).fetchone()

    d24 = c.execute(
        """
        SELECT COUNT(*) AS turns,
               SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) AS tokens
        FROM turns
        WHERE started_at >= datetime('now', '-24 hours')
        """
    ).fetchone()
    d24_errors = c.execute(
        """
        SELECT COUNT(DISTINCT tc.turn_id) AS n
        FROM tool_calls tc
        JOIN turns t ON t.id = tc.turn_id
        WHERE tc.is_error = 1
          AND t.started_at >= datetime('now', '-24 hours')
        """
    ).fetchone()
    # 24 hourly buckets keyed by offset (0 = 23h ago … 23 = current hour)
    bucket_rows = c.execute(
        """
        SELECT CAST((julianday('now') - julianday(started_at)) * 24 AS INTEGER) AS hours_ago,
               COUNT(*) AS n
        FROM turns
        WHERE started_at >= datetime('now', '-24 hours')
        GROUP BY hours_ago
        """
    ).fetchall()
    sparkline = [0] * 24
    for row in bucket_rows:
        ha = int(row["hours_ago"] or 0)
        if 0 <= ha < 24:
            sparkline[23 - ha] = int(row["n"])

    recent_rows = c.execute(
        """
        SELECT id, session_id, started_at, model, user_text
        FROM turns
        ORDER BY started_at DESC LIMIT 5
        """
    ).fetchall()
    recent_turns = []
    for r in recent_rows:
        snippet = (r["user_text"] or "").strip().replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:79] + "…"
        recent_turns.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "started_at": r["started_at"],
            "model": r["model"],
            "snippet": snippet,
        })

    top_cwd = hr_top_cwd["cwd"] if hr_top_cwd else None
    top_cwd_basename = top_cwd.rstrip("/").split("/")[-1] if top_cwd else None

    return JSONResponse({
        "server_now": server_now,
        "last_turn_at": last_at,
        "minutes_since_last_turn": minutes_since,
        "is_live": (minutes_since is not None and minutes_since < 5),
        "window_minutes": 60,
        "last_hour": {
            "turns": int(hr["turns"] or 0),
            "tokens": int(hr["tokens"] or 0),
            "errors": int(hr_errors["n"] or 0),
            "top_model": hr_top_model["model"] if hr_top_model else None,
            "top_cwd": top_cwd,
            "top_cwd_basename": top_cwd_basename,
        },
        "last_24h": {
            "turns": int(d24["turns"] or 0),
            "tokens": int(d24["tokens"] or 0),
            "errors": int(d24_errors["n"] or 0),
            "sparkline": sparkline,
        },
        "recent_turns": recent_turns,
    })


def _chart_endpoint(name: str):
    fn = CHARTS[name]
    def _handler():
        return JSONResponse(fn(connect()))
    _handler.__name__ = f"chart_{name}"
    return _handler


for _name in CHARTS:
    app.add_api_route(
        f"/api/charts/{_name}",
        _chart_endpoint(_name),
        methods=["GET"],
        response_class=JSONResponse,
        name=f"chart_{_name}",
    )


@app.get("/api/export")
def api_export(request: Request):
    conn = connect()
    params = dict(request.query_params)
    fmt = (params.pop("format", "jsonl") or "jsonl").lower()
    filters = parse_filters(params)
    if fmt == "csv":
        return StreamingResponse(
            export_csv(conn, filters),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="turns.csv"'},
        )
    if fmt == "jsonl":
        return StreamingResponse(
            export_jsonl(conn, filters),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="turns.jsonl"'},
        )
    return JSONResponse({"error": "format must be jsonl or csv"}, status_code=400)


# ─── Web Push notifications ────────────────────────────────────────────
from fastapi.responses import FileResponse


@app.get("/sw.js")
def serve_sw():
    """Serve the service worker at the site root so its scope is `/`."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/push/vapid-public-key", response_class=JSONResponse)
def push_vapid_public_key():
    from ..push_notify import vapid_public_key, ensure_vapid_keys
    ensure_vapid_keys()
    return JSONResponse({"key": vapid_public_key()})


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, _origin: None = Depends(require_same_origin)):
    from ..push_notify import register_subscription
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    sub = body.get("subscription", body)
    if not isinstance(sub, dict) or not isinstance(sub.get("endpoint"), str) or not sub.get("endpoint"):
        return JSONResponse({"error": "missing subscription endpoint"}, status_code=400)

    keys = sub.get("keys") or {}
    if not isinstance(keys, dict):
        return JSONResponse({"error": "subscription.keys must be an object"}, status_code=400)
    p256dh = keys.get("p256dh", "")
    auth = keys.get("auth", "")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        return JSONResponse({"error": "subscription.keys.p256dh/auth must be strings"}, status_code=400)

    user_agent = body.get("user_agent") or request.headers.get("user-agent")
    if user_agent is not None and not isinstance(user_agent, str):
        user_agent = None

    try:
        sub_id = register_subscription(
            connect(),
            endpoint=sub["endpoint"],
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent,
        )
    except ValueError as e:
        # Endpoint failed the push-service allowlist, or the subscription
        # cap was reached — both are clean client errors, not server bugs.
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": sub_id})


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, _origin: None = Depends(require_same_origin)):
    from ..push_notify import unregister_subscription
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if not isinstance(endpoint, str) or not endpoint:
        return JSONResponse({"error": "missing endpoint"}, status_code=400)
    ok = unregister_subscription(connect(), endpoint=endpoint)
    return JSONResponse({"removed": ok})


# ─── Classifier (sklearn fallback grader) ──────────────────────────────
from ..ai.classifier import GraderClassifier


@app.get("/api/classifier/status", response_class=JSONResponse)
def classifier_status():
    return JSONResponse(GraderClassifier().latest_model_meta() or {"trained": False})


# ─── Ask-the-Ledger (Round B1) ─────────────────────────────────────────
@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    from ..insights import questions_for_chips, categories
    return _render("ask.html", {
        "q": "",
        "request": request,
        "ask_categories": categories(),
        "ask_questions": questions_for_chips(),
    })


@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request):
    """Glance-fast view of the pre-computed self-reflection answers.
    Runs zero LLM calls on render — reads data/insights.json. Stale
    cache → the page still renders, with a 'compute now' affordance."""
    from ..insights import load_cached, categories, CURATED_QUESTIONS
    cached = load_cached()
    return _render("insights.html", {
        "q": "",
        "request": request,
        "categories": categories(),
        "questions": [q.__dict__ for q in CURATED_QUESTIONS],
        "computed_at": cached.get("computed_at", 0),
        "entries": cached.get("entries") or {},
    })


@app.post("/api/insights/recompute")
async def api_insights_recompute(background: BackgroundTasks,
                                  force: bool = False,
                                  _origin: None = Depends(require_same_origin)):
    """Kick the compute in the background so the click is instant.
    The /insights page polls the cache file's mtime to know when
    it's done."""
    from ..insights import compute_all

    def _run():
        conn = connect()
        try:
            compute_all(conn, force_refresh=force, progress=lambda *_: None)
        except Exception as e:  # noqa: BLE001
            import sys as _s; print(f"[insights] background failed: {e}",
                                      file=_s.stderr)

    background.add_task(_run)
    return JSONResponse({"status": "computing"})


@app.post("/api/ask")
async def api_ask(request: Request, _origin: None = Depends(require_same_origin)):
    """Stream the Q&A pipeline as SSE: plan → sources → delta → done."""
    import json as _json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    question = (body.get("question") or "").strip() if isinstance(body, dict) else ""
    if not question:
        return JSONResponse({"error": "missing question"}, status_code=400)

    def _gen():
        from ..ai.qa import stream_answer
        conn = connect()
        try:
            for evt in stream_answer(conn, question):
                yield f"data: {_json.dumps(evt)}\n\n"
        finally:
            try: conn.close()
            except Exception: pass

    return StreamingResponse(_gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})


# ─── Smart Rerun Queue (Round B2) ──────────────────────────────────────
@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    from ..ai.queue import score_candidates
    candidates = score_candidates(connect(), k=20)
    return _render("queue.html", {"candidates": candidates, "q": ""})


@app.get("/api/queue", response_class=JSONResponse)
def api_queue():
    from ..ai.queue import score_candidates
    return JSONResponse({"candidates": score_candidates(connect(), k=20)})


# ─── Suggest expected_behavior (Round C2) ──────────────────────────────
@app.post("/api/annotation/suggest_expected/{turn_id}", response_class=JSONResponse)
def api_annotation_suggest(turn_id: int, _origin: None = Depends(require_same_origin)):
    from ..ai.annotate import suggest_expected
    text = suggest_expected(connect(), turn_id)
    if not text:
        return JSONResponse({"error": "could not generate suggestion"}, status_code=503)
    return JSONResponse({"expected_behavior": text})


# ─── Prompt diet (Round C3) ────────────────────────────────────────────
@app.get("/api/diet/cluster/{cluster_id}", response_class=JSONResponse)
def api_diet_for_cluster(cluster_id: int):
    rows = connect().execute(
        "SELECT * FROM prompt_diet_suggestions WHERE cluster_id = ? ORDER BY id DESC",
        (cluster_id,),
    ).fetchall()
    return JSONResponse({"suggestions": [dict(r) for r in rows]})


@app.post("/api/diet/cluster/{cluster_id}/propose")
def api_diet_propose(cluster_id: int, _origin: None = Depends(require_same_origin)):
    from ..ai.diet import propose_for_cluster
    try:
        r = propose_for_cluster(connect(), cluster_id)
    except Exception as e:
        # All LLM tiers failed (e.g. exhausted budget / no backend produced
        # valid JSON) — surface a clean 502 instead of an uncaught 500.
        return JSONResponse({"error": f"propose failed: {e}"}, status_code=502)
    if not r:
        return JSONResponse({"error": "could not propose"}, status_code=503)
    return JSONResponse(r)


# ─── Daily digest (Round C1) ───────────────────────────────────────────
@app.get("/api/digest/today", response_class=JSONResponse)
def api_digest_today():
    from datetime import date
    conn = connect()
    try:
        row = conn.execute(
            "SELECT date, summary, model, cost_usd, generated_at FROM daily_digests "
            "WHERE date = ? ORDER BY generated_at DESC LIMIT 1",
            (date.today().isoformat(),),
        ).fetchone()
    except Exception:
        # daily_digests table doesn't exist yet (no digest has been generated)
        row = None
    if not row:
        return JSONResponse({"date": date.today().isoformat(), "summary": None})
    return JSONResponse(dict(row))


@app.post("/api/digest/generate")
async def api_digest_generate(request: Request, background: BackgroundTasks,
                               _origin: None = Depends(require_same_origin)):
    """Trigger digest generation in the background. Returns 202."""
    body = {}
    try: body = await request.json()
    except Exception: pass
    day = body.get("date")
    notify_push = bool(body.get("notify"))

    def _go():
        from ..ai.digest import generate
        try: generate(connect(), day=day, notify_push=notify_push)
        except Exception: pass

    background.add_task(_go)
    return JSONResponse({"status": "scheduled"}, status_code=202)


MAX_QUEUE_RERUN_ALL = 50  # cap: unbounded turn_ids would queue unbounded billed jobs


@app.post("/api/queue/rerun-all")
async def api_queue_rerun_all(request: Request, background: BackgroundTasks,
                               _origin: None = Depends(require_same_origin)):
    form = await request.form()
    raw_ids = form.getlist("turn_ids")
    turn_ids: list[int] = []
    for s in raw_ids:
        try: turn_ids.append(int(s))
        except ValueError: continue
    if not turn_ids:
        return RedirectResponse("/queue", status_code=303)
    if len(turn_ids) > MAX_QUEUE_RERUN_ALL:
        return JSONResponse(
            {"error": f"too many turn_ids (max {MAX_QUEUE_RERUN_ALL})"},
            status_code=400,
        )

    def _run_all(ids: list[int]) -> None:
        from ..rerun import run_rerun
        for tid in ids:
            try:
                run_rerun(tid, budget_usd=0.50)
            except Exception:
                continue

    background.add_task(_run_all, turn_ids)
    return RedirectResponse(f"/queue?queued={len(turn_ids)}", status_code=303)


@app.get("/api/classifier/predict/{turn_id}", response_class=JSONResponse)
def classifier_predict(turn_id: int):
    return JSONResponse(GraderClassifier().predict(connect(), turn_id))


def _run_rerun_safely(turn_id: int, model: str | None, budget_usd: float) -> None:
    try:
        run_rerun(turn_id, model=model, budget_usd=budget_usd)
    except Exception:
        import traceback
        from .. import LOG_PATH
        try:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"[rerun] turn_id={turn_id} failed:\n{traceback.format_exc()}\n")
        except Exception:
            pass


# Hard cap on the caller-controlled budget forwarded verbatim to a real
# billed `claude` subprocess call: 10x the operator's own configured default
# (RERUN_BUDGET_USD / DEFAULT_BUDGET_USD) is generous headroom for a
# deliberate one-off without leaving the cap unbounded.
MAX_RERUN_BUDGET_USD = DEFAULT_BUDGET_USD * 10


@app.post("/api/rerun/{turn_id}")
def trigger_rerun(
    turn_id: int,
    background: BackgroundTasks,
    budget_usd: float = Form(0.50),
    model: str = Form(""),
    _origin: None = Depends(require_same_origin),
):
    # Validate the caller-controlled budget before touching the DB — a bad
    # value is a client error regardless of whether turn_id exists.
    if not (0 < budget_usd <= MAX_RERUN_BUDGET_USD):
        return JSONResponse(
            {"error": f"budget_usd must be > 0 and <= {MAX_RERUN_BUDGET_USD:g} "
                      f"(10x the {DEFAULT_BUDGET_USD:g} default)"},
            status_code=400,
        )
    conn = connect()
    exists = conn.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if not exists:
        return JSONResponse({"error": "turn not found"}, status_code=404)
    background.add_task(_run_rerun_safely, turn_id, model.strip() or None, float(budget_usd))
    return RedirectResponse(f"/turns/{turn_id}", status_code=303)


@app.get("/turns/{turn_id}/reruns", response_class=HTMLResponse)
def turn_reruns(request: Request, turn_id: int):
    conn = connect()
    turn = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if not turn:
        return HTMLResponse("turn not found", status_code=404)
    turn = dict(turn)
    turn.pop("raw_json_z", None)
    reruns = [dict(r) for r in conn.execute(
        "SELECT * FROM reruns WHERE original_turn_id = ? ORDER BY run_at DESC",
        (turn_id,),
    ).fetchall()]
    return _render("reruns_list.html", {"turn": turn, "reruns": reruns, "q": ""})


@app.get("/turns/{turn_id}/diff/{rerun_id}", response_class=HTMLResponse)
def turn_rerun_diff(request: Request, turn_id: int, rerun_id: int):
    conn = connect()
    turn = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    rerun = conn.execute(
        "SELECT * FROM reruns WHERE id = ? AND original_turn_id = ?",
        (rerun_id, turn_id),
    ).fetchone()
    if not turn or not rerun:
        return HTMLResponse("turn or rerun not found", status_code=404)
    turn = dict(turn); turn.pop("raw_json_z", None)
    rerun = dict(rerun)
    diff_html = render_inline_diff(turn.get("assistant_text") or "", rerun.get("response_text") or "")
    judgment_row = conn.execute(
        "SELECT * FROM rerun_judgments WHERE rerun_id = ?", (rerun_id,)
    ).fetchone()
    judgment = None
    if judgment_row:
        judgment = dict(judgment_row)
        try:
            from ..raw_archive import decompress_json
            judgment["dimensions"] = decompress_json(judgment.pop("dimensions_json", None)) or {}
        except Exception:
            judgment["dimensions"] = {}
    return _render("diff.html", {
        "turn": turn, "rerun": rerun, "diff_html": diff_html,
        "judgment": judgment, "q": "",
    })


# ─── Round D1: AI port classifier ────────────────────────────────────────
def _classify_ports_api_key() -> str:
    """Resolve a real Anthropic API key for the port-classifier feature.

    Order: ANTHROPIC_API_KEY env → key file (TELEMETRIFY_ANTHROPIC_KEY_FILE
    or ~/.ai-spending/anthropic_key). Deliberately does NOT accept the Claude
    Code OAuth token (ANTHROPIC_AUTH_TOKEN) — this feature must use a billed
    sk-ant key, which has the throughput the OAuth subscription lacks.
    """
    import os
    from pathlib import Path
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    candidates = [
        os.environ.get("TELEMETRIFY_ANTHROPIC_KEY_FILE"),
        str(Path.home() / ".ai-spending" / "anthropic_key"),
    ]
    for path in candidates:
        if not path:
            continue
        try:
            v = Path(path).read_text().strip()
            if v:
                return v
        except OSError:
            continue
    return ""


@app.post("/api/classify-ports", response_class=JSONResponse)
async def api_classify_ports(request: Request, _origin: None = Depends(require_same_origin)):
    """Classify a batch of unknown listening ports using the LLM router.

    Body:
      { "ports": [
          {"port": 5555,
           "proc": "",
           "command": "",
           "user": "",
           "probe": "non-http: Empty reply"},
          ...
      ]}

    Response:
      { "classifications": [
          {"port": 5555, "kind": "zmq", "label": "ZeroMQ socket",
           "confidence": "high", "reasoning": "binary protocol on
                                              ZMQ default port"},
          ...
      ],
        "backend": "rocco" | "localmac" | "ollama" | "anthropic",
        "cost_usd": 0.00012
      }

    Uses the same LLM router as `/ask` — Rocco vLLM first when up,
    then Mac-local Ollama, then Ollama Cloud, then Anthropic. Always
    Haiku-class (cheap, fast) — port classification is not a
    reasoning-heavy task.
    """
    import json as _json
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    raw_ports = body.get("ports")
    if not isinstance(raw_ports, list) or not raw_ports:
        return JSONResponse({"error": "ports[] required"}, status_code=400)

    # Trim to a sane size — the prompt grows with N and Haiku has limits.
    raw_ports = raw_ports[:25]

    # Render the ports block — each port becomes one bullet for the LLM.
    bullets = []
    for p in raw_ports:
        if not isinstance(p, dict):
            # A malformed-but-JSON-valid item (e.g. a bare string/int in the
            # list) has no `.get` — skip it instead of crashing with 500.
            continue
        try:
            port = int(p.get("port"))
        except (TypeError, ValueError):
            continue
        proc = str(p.get("proc") or "").strip()
        cmd = str(p.get("command") or "").strip()[:120]
        user = str(p.get("user") or "").strip()
        probe = str(p.get("probe") or "").strip()[:200]
        line = f"- port {port}"
        if proc:  line += f"  proc={proc!r}"
        if user:  line += f"  user={user!r}"
        if cmd:   line += f"  command={cmd!r}"
        if probe: line += f"\n    probe: {probe!r}"
        bullets.append(line)
    if not bullets:
        return JSONResponse({"error": "no valid ports"}, status_code=400)

    from ..ai.client import AnthropicClient
    from ..ai import prompts as P

    # This feature is PINNED to Claude Opus 4.7 via a real Anthropic API key,
    # routed through the ai-spend-proxy so the cost is tracked. Rationale:
    #   - port classification is structured-JSON output; local models
    #     (Rocco/Kimi) reliably return free-form text ("---") and the Claude
    #     Code OAuth token gets 429-rate-limited — both produce the "AI gave
    #     up" failure the user hit.
    #   - so: real sk-ant key + Opus 4.7, no local fallback. A failure is
    #     surfaced honestly rather than silently degrading to a local model.
    api_key = _classify_ports_api_key()
    if not api_key:
        return JSONResponse(
            {"classifications": [],
             "error": "no Anthropic API key configured — port classification "
                      "requires a real sk-ant key (set ANTHROPIC_API_KEY for "
                      "the telemetrify server or write ~/.ai-spending/anthropic_key)",
             "raw_preview": ""},
            status_code=200,
        )

    import os
    # Route through the local spend-proxy (live-spending-tracker) so every
    # Opus call is metered. Override with TELEMETRIFY_CLASSIFY_BASE_URL.
    base_url = os.environ.get(
        "TELEMETRIFY_CLASSIFY_BASE_URL", "http://localhost:7778/anthropic"
    )

    def _call_opus(extra_suffix: str = "") -> str:
        import anthropic
        sys_prompt, user_prompt = P.CLASSIFY_PORTS.render(
            ports_block="\n".join(bullets) + extra_suffix
        )
        sdk = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=45.0)
        resp = sdk.messages.create(
            # Generous budget: a 25-port batch with per-port reasoning can
            # exceed 900 tokens and truncate the JSON mid-array ("Expecting
            # ',' delimiter"). Opus is the floor here, so give it room.
            model="claude-opus-4-7",
            max_tokens=3000,
            system=sys_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = ""
        for block in (resp.content or []):
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "")
        return text

    raw = ""
    parsed: dict = {}
    try:
        raw = _call_opus()
        parsed = AnthropicClient._extract_json(raw)
    except Exception as first_err:
        # One retry with a stricter "JSON ONLY" nudge before giving up.
        try:
            raw = _call_opus(
                "\n\nIMPORTANT: Respond with JSON only. "
                "First character of your output MUST be `{`. "
                "Do not include any commentary, no '---', no markdown."
            )
            parsed = AnthropicClient._extract_json(raw)
        except Exception as second_err:
            # Hard-fail: never fall back to a local model for this feature.
            return JSONResponse(
                {"classifications": [],
                 "error": f"Opus 4.7 unavailable: {second_err}",
                 "raw_preview": (raw or str(second_err))[:200]},
                status_code=200,
            )

    parsed.setdefault("classifications", [])
    parsed["backend"] = "anthropic"
    parsed["model_used"] = "claude-opus-4-7"
    return JSONResponse(parsed)
