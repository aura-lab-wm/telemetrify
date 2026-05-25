# telemetrify

<p align="left">
  <img src="assets/brand/mark.svg" width="56" alt="telemetrify mark">
</p>

> **Per-prompt telemetry capture, semantic search, and replay for Claude Code sessions.** Local-first.

`telemetrify` is a no-cloud pipeline for Claude Code. Every turn — prompt, response, tool calls, tokens, attribution, thinking, full raw archive — lands in a local SQLite DB the moment the assistant finishes a turn. A FastAPI UI on `localhost:8767` lets you semantically search, filter, cluster, annotate, replay-and-diff, and chart the corpus.

No third-party services required for capture. The whole thing runs out of `~/Projects/telemetrify`.

> **Heads-up — renamed.** This project was previously `prompt-telemetry` (Python module `prompt_telemetry`). The Python package is now `telemetrify`; a thin back-compat shim keeps `import prompt_telemetry` working with a `DeprecationWarning`. Update your imports, then drop `src/prompt_telemetry/` whenever you're ready.

---

## Project shape — telemetrify + rocco-pulse + rocco-agent

Telemetrify is **three programs that ship from this one repo**, not a single monolith. They cooperate so the "ask the corpus" feature (`/ask`) can run a large model (Kimi-Dev-72B BF16, ~145 GB) on a remote GPU box without you ever leaving the Mac.

```
┌──────────────────────────── this repo (~/Projects/telemetrify) ────────────────────────────┐
│                                                                                            │
│   src/telemetrify/        ←  the Python pipeline + FastAPI UI on localhost:8767            │
│                              everything that captures turns, embeds, clusters, asks        │
│                                                                                            │
│   menubar/                ←  TWO subordinate programs that exist BECAUSE telemetrify       │
│                              wants to run heavy LLM inference on a remote GPU server:      │
│                                                                                            │
│     RoccoPulse{App,Core}  ←  • macOS menubar app (Swift / SwiftUI MenuBarExtra)            │
│                                "is Rocco up? which model is loaded? which tier?            │
│                                 is /ask going to land on Rocco or fall back to Anthropic?" │
│                                                                                            │
│     rocco-agent/          ←  • Python daemon that runs ON Rocco itself (systemd --user)    │
│                                Polls nvidia-smi + model_manager every 5s, writes           │
│                                ~/.cache/rocco-status.json. SSH-readable by the Mac.        │
│                                                                                            │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Why rocco-pulse is part of telemetrify, not a separate project.** Telemetrify's `/ask` page routes every QA call through a 4-tier LLM stack (Rocco vLLM → Mac-local Ollama → Ollama Cloud → Anthropic). Rocco is the *primary* tier — it's the only one that's free, fast, and serves a 72B-parameter model. But Rocco is a shared lab GPU box: vLLM dies when other users grab the cards, comes back when they release them, and you have no way to know which from the Mac without SSHing in.

`rocco-pulse` is the operational dashboard for telemetrify's Rocco backend. Without it you'd:
- not know when `/ask` is silently falling back to your Anthropic OAuth quota (expensive, slow)
- not know which model `/ask` is actually using right now
- have to SSH to Rocco to start/stop vLLM by hand
- have no live read on GPU util / mem / temperature on the lab box

With it, the menubar always tells you the tier + the running model + GPU stats, and the popover has a one-click Start/Stop for the remote vLLM. Same data flows the prompt-submit hook reads, so the hook and the menubar can never disagree.

If you only ever use telemetrify with `ANTHROPIC_API_KEY` (no remote GPU box), rocco-pulse is unused and harmless. Skip building it.

---

## What it captures

For every assistant turn, the Stop hook records:

- prompt + response (markdown), thinking text
- model, cli version, attribution skill/plugin
- input / output / cache-creation / cache-read tokens
- latency (ms), tool-call count, full per-call inputs+outputs
- cwd, git branch, entrypoint, user type
- lossless zstd-compressed JSON archive of the underlying transcript records
- 384-dim MiniLM embeddings of (prompt+response) and prompt-only

---

## Architecture

```
~/Projects/telemetrify/
├── pyproject.toml             # uv-managed
├── README.md
├── data/                      # gitignored
│   ├── prompts.db             # SQLite + sqlite-vec, WAL mode
│   ├── backups/               # auto-snapshots before destructive migrations
│   ├── reruns/                # one workspace per rerun (cmd.json + stdout.json)
│   └── capture.log
├── src/telemetrify/
│   ├── migrations/00{1..7}_*.{sql,py}   # versioned schema, fcntl-locked applier
│   ├── db.py                  # connect() applies migrations
│   ├── transcript.py          # JSONL → Turn dataclass
│   ├── store.py               # upsert_session, insert_turn
│   ├── capture.py             # Stop-hook entry (skips sub-agents, idempotent)
│   ├── embed.py               # MiniLM singleton; embed / embed_prompt / embed_batch
│   ├── raw_archive.py         # zstd compress / decompress helpers
│   ├── search.py              # FTS5+vec hybrid via RRF + filter parser
│   ├── followups.py           # within-session retry / paraphrase detection
│   ├── cluster.py             # HDBSCAN over prompt embeddings
│   ├── rerun.py               # claude -p subprocess driver + difflib HTML diff
│   ├── doctor.py              # health checks (CLI + /api/health)
│   ├── charts.py + export.py  # dashboard data + CSV/JSONL streaming
│   ├── backends/              # sqlite (real) + postgres (stub)
│   ├── sync.py                # bin/sync — dialect-translates migrations
│   └── ui/                    # FastAPI + Jinja2 + a 24-icon SVG sprite
└── bin/
    ├── capture-hook           # wired into ~/.claude/settings.json Stop hook
    ├── telemetry-ui           # uvicorn → http://127.0.0.1:8767
    ├── backfill               # ingest every transcript on disk
    ├── recluster              # rebuild prompt clusters
    ├── rerun TURN_ID          # replay a past prompt against current claude CLI
    ├── doctor                 # one-screen health check (exit 0/1)
    └── sync --target ...      # postgres dry-run schema preview (stub)
```

---

## Setup

```bash
cd ~/Projects/telemetrify
uv sync
```

Wire the Stop hook into `~/.claude/settings.json` (preserving any existing hooks):

```json
"hooks": {
  "Stop": [
    {
      "hooks": [
        { "type": "command", "command": "<ABSOLUTE_PATH>/bin/capture-hook" }
      ]
    }
  ]
}
```

Optionally backfill historical sessions (`~/.claude/projects/**/*.jsonl`):

```bash
bin/backfill --no-embed       # fast metadata-only pass
python -m telemetrify.embed_missing
python -m telemetrify.backfill_prompt_vec
bin/recluster
python -m telemetrify.followups   # in the package; see below for CLI form
```

Run the UI:

```bash
bin/telemetry-ui              # http://127.0.0.1:8767
PORT=9000 bin/telemetry-ui    # custom port
```

---

## Features

### Capture (passive)
- Stop hook → reads session JSONL → one row in `turns`, N rows in `tool_calls`, two embeddings (full-turn + prompt-only), nearest-cluster assignment, follow-up detection.
- Skips sub-agent Stop events via the `agent_id` payload field.
- Idempotent (UNIQUE constraint on `user_uuid`).
- Failures land in `data/capture.log`; the hook always exits 0 so it can't block your shell.

### Hybrid retrieval
- FTS5 BM25 (porter unicode61) over user_text + assistant_text.
- sqlite-vec cosine KNN over MiniLM embeddings.
- Reciprocal Rank Fusion (RRF, k=60) combines both ranks.
- Whitelisted filter bar: model, cwd glob, skill, cluster, origin, since/until, has_error/has_followup/has_annotation, min/max tokens, min/max latency.

### Telemetry-testing primitives
- **Annotations** — rating (-1/0/+1), label, tags, expected_behavior, notes per turn.
- **Prompt clusters** — HDBSCAN over prompt-only embeddings; stable cluster ids across reruns via Hungarian-style centroid matching. At capture time, each new turn is assigned to the nearest existing cluster within cosine ≤ 0.30.
- **Follow-up detection** — within-session retry/correction signal from prompt distance < 0.40 **or** regex match on corrective lead-ins (`no, …`, `actually …`, `wait …`, etc).
- **Rerun-and-diff** — `bin/rerun TURN_ID` invokes `claude -p` headlessly with `--max-budget-usd` cap, persists the response into `reruns`, exposes inline HTML diff at `/turns/{id}/diff/{rerun_id}`.

### Analysis dashboard
- 8 Plotly charts at `/dashboard`: turns/day, tokens by model (stacked), tool heatmap, tool-error rate, latency p50/p95, annotation breakdown, correction-rate trend, top-10 clusters.
- Similar-turns sidebar on every `/turns/{id}` (KNN, excluding same-session).
- Bulk export at `/api/export?format=jsonl|csv&...filters...` streams.

### Operations
- `bin/doctor` — schema version, lag since last capture, vector/FTS/cluster coverage, hook wiring sanity, recent errors. Exit 0 healthy / 1 unhealthy.
- `/api/health` — JSON form for dashboard banner.
- Migration framework — numbered SQL + Python files in `migrations/`, applied via fcntl-locked runner; destructive migrations take an automatic file backup.

### Cloud-sync scaffold (stub)
- `bin/sync --target postgresql://... --dry-run` dialect-translates all migrations to Postgres + pgvector (`BIGSERIAL`, `vector(384)`, FTS→tsvector TODO).
- Writes raise `NotImplementedError` with a credential-masked DSN — wire-up is the only deferred work.

---

## Direct SQL for analysis

```sql
-- turns per day
SELECT date(started_at) d, COUNT(*) n FROM turns GROUP BY d ORDER BY d;

-- most-used tools by week
SELECT strftime('%Y-W%W', t.started_at) wk, tc.tool_name, COUNT(*) n
FROM tool_calls tc JOIN turns t ON t.id = tc.turn_id
GROUP BY wk, tool_name ORDER BY wk, n DESC;

-- cost-relevant token totals by model
SELECT model, SUM(input_tokens) in_t, SUM(output_tokens) out_t,
       SUM(cache_creation_tokens) ccr, SUM(cache_read_tokens) crd, COUNT(*) turns
FROM turns GROUP BY model;

-- prompts you keep retrying (top regression candidates)
SELECT pc.id, pc.label, COUNT(f.turn_id) AS followups, pc.member_count
FROM prompt_clusters pc
JOIN turn_cluster tc ON tc.cluster_id = pc.id
JOIN turn_followups f ON f.turn_id = tc.turn_id
GROUP BY pc.id ORDER BY followups DESC LIMIT 20;
```

---

## LLM backends (Rocco vLLM → Ollama Cloud → Anthropic)

Every AI feature (`/ask`, grader, digest, queue rationale, cluster labels, rerun-judge, annotate, diet) now flows through `BackendRouter` in `src/telemetrify/ai/router.py`. The router tries backends in order; transport errors fall through, deterministic errors (4xx, budget cap, schema-parse) do not. Each attempted backend writes one row to `ai_runs` with a new `backend` column (`rocco` | `ollama` | `anthropic`).

```
TELEMETRIFY_LLM_ORDER=rocco,ollama,anthropic          # default; first available wins
TELEMETRIFY_LLM_ORDER__grader=anthropic               # per-feature pin (double underscore)
ROCCO_BASE_URL=http://localhost:18000/v1              # SSH-tunnel default
ROCCO_MODEL=moonshotai/Kimi-Dev-72B
ROCCO_API_KEY=EMPTY                                   # vLLM accepts any
OLLAMA_CLOUD_BASE_URL=https://ollama.com/v1
OLLAMA_CLOUD_API_KEY=...                              # absent → backend reports unavailable
OLLAMA_CLOUD_MODEL=gpt-oss:120b
ANTHROPIC_AUTH_TOKEN=...                              # unchanged safety net
```

The daily $ cap (`AI_BUDGET_USD_PER_DAY`) counts only Anthropic spend. Local-tier runs record a synthetic micro-cost so the dashboard's cost chart still distinguishes "Rocco ran" from "no AI ran" but rolls up to ~$0.00001/day.

**Rocco access** is over SSH (`rocco.cs.wm.edu:13110`). Keep a long-running ControlMaster tunnel up:
```sh
# in ~/.ssh/config under: Host rocco
#   ControlMaster auto / ControlPath ~/.ssh/cm-%r@%h:%p / ControlPersist 10m

ssh -L 18000:localhost:8000 -fN rocco                 # one-time, lives 10 min after disconnect
```

**rocco-pulse** (the menubar app described in the architecture section above) is the GUI for this same backend stack — same tier order, same `model_manager` source of truth. If you're running telemetrify against Rocco, install it: `bash menubar/rocco-agent/install.sh rocco` then `make -C menubar install`. If you're Anthropic-only, skip it.

---

## Limitations

- **Local-only capture**: the Stop hook reads `~/.claude/projects/` on the local Mac. The deployed/synced viewer cannot capture — it can only display rows pushed via `bin/sync`.
- **Rerun fidelity**: `claude -p --no-session-persistence` still reads global `~/.claude/settings.json` (hooks, MCPs, env). Truly hermetic replay would need `CLAUDE_CONFIG_DIR=` isolation — out of scope for now.
- **Rerun budget**: the CLI accumulates cache-creation tokens before checking `--max-budget-usd`, so the effective cost can overshoot the cap by 2–3×. The DB records the actual spend in `reruns.total_cost_usd`.

---

## License

Private. Not for redistribution.
