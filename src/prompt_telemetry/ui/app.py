from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
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
from ..rerun import run_rerun, render_inline_diff

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"

_env = Environment(
    loader=FileSystemLoader(str(HERE / "templates")),
    autoescape=select_autoescape(["html"]),
)

app = FastAPI(title="Prompt Telemetry")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_md = MarkdownIt("commonmark", {"breaks": True, "linkify": True})


def render_md(text: str) -> str:
    return _md.render(text or "")


def _snippet(text: str, limit: int = 280) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


_env.filters["md"] = render_md
_env.filters["snippet"] = _snippet


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
    turns = conn.execute(
        "SELECT * FROM turns WHERE session_id = ? ORDER BY started_at ASC",
        (session_id,),
    ).fetchall()
    return _render("session.html", {
        "session": dict(session) if session else None,
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
def delete_annotation(turn_id: int):
    conn = connect()
    with conn:
        conn.execute("DELETE FROM annotations WHERE turn_id = ?", (turn_id,))
    return RedirectResponse(f"/turns/{turn_id}", status_code=303)


@app.get("/clusters", response_class=HTMLResponse)
def cluster_index(request: Request):
    conn = connect()
    rows = conn.execute(
        """
        SELECT pc.id, pc.label, pc.member_count, pc.representative_turn_id, pc.updated_at,
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


@app.post("/api/rerun/{turn_id}")
def trigger_rerun(
    turn_id: int,
    background: BackgroundTasks,
    budget_usd: float = Form(0.50),
    model: str = Form(""),
):
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
    return _render("diff.html", {"turn": turn, "rerun": rerun, "diff_html": diff_html, "q": ""})
