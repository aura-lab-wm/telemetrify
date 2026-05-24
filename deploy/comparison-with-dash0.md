# prompt-telemetry vs dash0-agent-plugin

## TL;DR

- **prompt-telemetry** — your local, single-binary Python pipeline that grades, clusters, paraphrase-detects, and rerun-diffs every Claude Code turn into one SQLite-vec DB on this Mac. Built around the **research/analysis** loop.
- **dash0-agent-plugin** — a Go binary shipping ~24 Claude Code event types as OTel-conformant GenAI traces/logs to an OTLP/HTTP endpoint (Dash0 cloud by default). Built around the **production-ops/multi-agent timeline** loop.
- **Single most important difference:** prompt-telemetry captures _one event_ (Stop) but enriches it heavily; dash0 captures _many events_ flatly. prompt-telemetry knows things about your prompts; dash0 knows things about your sessions' shape.

## Capture surface

| Hook event | prompt-telemetry | dash0 |
|---|---|---|
| SessionStart / SessionEnd | — | yes |
| UserPromptSubmit | optional | yes |
| Stop / StopFailure | **Stop only** | both |
| PreToolUse / PostToolUse / PostToolUseFailure | — (reconstructed from JSONL post-hoc) | yes |
| PermissionRequest / PermissionDenied | — | yes |
| SubagentStart / SubagentStop | skipped intentionally | yes |
| TaskCreated / TaskCompleted / TeammateIdle | — | yes |
| InstructionsLoaded / ConfigChange / CwdChanged / FileChanged | — | yes |
| PreCompact / PostCompact | — | yes |
| Elicitation / ElicitationResult / Notification | — | yes |
| **Distinct events** | **1–2** | **24** |

prompt-telemetry sees ~5–8% of the live event surface dash0 sees. Crucially, it does reconstruct tool calls (and their I/O, errors, timing) from `~/.claude/projects/*.jsonl` inside the Stop hook, so it's not missing that data — but everything it knows arrives in one batch after each turn, never mid-flight.

## Data model

| Dimension | prompt-telemetry | dash0 |
|---|---|---|
| Shape | Relational: `sessions` → `turns` → `tool_calls` + `turn_vec` (vec0 384-dim) + `turns_fts` (FTS5) + `annotations` / `prompt_clusters` / `turn_followups` / `reruns` / `auto_grades` | OTel: `resourceSpans` → `scopeSpans` → `spans` + span events + log records, GenAI semconv attribute names |
| Granularity | 1 row per assistant turn | 1 span per event (LLM span, tool span, session span, …) |
| Tool call shape | nested into the turn | sibling span |
| Token usage | columns on `turns` | `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read_*` / `cache_creation_*` |
| Raw archive | zstd-compressed JSONL slice on every turn (`raw_json_z`) | none locally — only OTLP payload |

Overlap: token counts, tool name+args+result, model, session id, git branch, cwd, latency. Divergence: prompt-telemetry has prompt+response text, MiniLM embeddings, clustering, and annotations; dash0 has the full multi-event timeline (compaction, permissions, subagents, teammates) that prompt-telemetry can't see from its Stop-only vantage.

## Storage + transport

- **prompt-telemetry**: SQLite-vec WAL DB at `data/prompts.db`. Zero network. Stop hook → local write → done. Privacy by inertia: data is on this Mac unless `bin/sync` pushes to your Postgres.
- **dash0**: spawns Go binary on every event → builds OTLP/HTTP JSON → POSTs to `DASH0_OTLP_URL` (default Dash0 SaaS). Also writes raw event payloads to `~/.claude/plugins/data/dash0-agent-plugin/events.jsonl` as a side log. Can be pointed at a local OTLP collector — and that is what makes the integration in §9 work.

## Search / analysis features

Unique to **prompt-telemetry**:

- Hybrid retrieval: FTS5 BM25 ∪ sqlite-vec cosine fused via Reciprocal Rank Fusion (k=60), 50/side fanout.
- MiniLM-L6-v2 embeddings for both full-turn and prompt-only similarity.
- HDBSCAN clustering with stable cluster IDs across rebuilds (Hungarian-style centroid matching); new turns assigned to nearest existing cluster at capture-time if ≤ 0.30 cosine.
- Follow-up / retry detection (paraphrase distance < 0.40 **or** corrective-lead-in regex).
- `bin/rerun TURN_ID` headless `claude -p` replay with `--max-budget-usd` cap + side-by-side HTML diff against the original.
- LLM-as-Judge auto-grading on every Stop and on every rerun, plus a trainable classifier registry (`ai/classifier.py`, `015_classifier_state.sql`) for cheap predicted-grade fan-out.
- Plotly dashboard (turns/day, tokens by model, tool heatmap, tool-error rate, latency p50/p95, correction-rate trend, top-10 clusters).
- Web Push notifications (`push_notify*.py`, migration 014) for ingest events.
- Bulk JSONL/CSV export with the same whitelisted filter parser.

Unique to **dash0**:

- Hosted Dash0 observability UI (you have an account already with the SaaS).
- OTel GenAI semconv standardization → portable to **any** OTel backend (Honeycomb, Tempo, Grafana, Datadog, self-hosted collector).
- Multi-agent / Task / subagent / teammate timeline — the only one of the two that can render a parent-Task fan-out as a real trace tree across spans.
- Compaction / permission / file-change events surfaced as first-class spans.
- Anonymization plumbing built into the wire format.

## Privacy defaults

| | prompt-telemetry | dash0 |
|---|---|---|
| Prompt text | stored raw + zstd raw archive | **omitted** (`OMIT_IO=true`) |
| Tool I/O | stored raw | **omitted** (`OMIT_IO=true`, 16 KB cap when on) |
| Username | not collected | **SHA-256 hashed** (`OMIT_USER_INFO=true`); email never sent |
| Repo / branch | stored | sent as `dash0.gen_ai.vcs.*` |
| Network | none | OTLP/HTTP to remote |

prompt-telemetry is privacy-by-locality; dash0 is privacy-by-redaction. Different threat models.

## Cost

- **prompt-telemetry**: $0 ongoing. CPU at capture (MiniLM embed + HDBSCAN nearest + optional LLM-judge — judge spend is on your Anthropic key).
- **dash0**: SaaS with a free tier; needs an account, an `AUTH_TOKEN` in keychain, and an `OTLP_URL`. Self-hostable in principle since it's pure OTLP.

## When to use which

- **prompt-telemetry** when you want to: search "what did I ask Claude about HDBSCAN last month", cluster paraphrased prompts, replay a regression against today's CLI, grade a turn with an LLM jury, train a classifier from your annotations, or answer SQL questions about your own habits — all offline.
- **dash0** when you want to: see a multi-agent fan-out as a trace timeline with compaction, permission, and subagent spans; collaborate with someone else on a session; or feed Claude Code activity into an existing OTel pipeline alongside your other services.
- **Both** when you want: dash0's wide event surface **and** prompt-telemetry's search/cluster/rerun loop on the same data. That's what migration 016 enables.

## Integration potential

`src/prompt_telemetry/migrations/016_dash0_otel.sql` (already in this repo) adds four additive tables: `dash0_resources`, `dash0_spans`, `dash0_span_events`, `dash0_log_records`. They join back into the existing schema on `dash0_spans.conversation_id == sessions.id` (Claude session UUID).

Wiring: stand up a local OTLP/HTTP sink that writes those tables; point dash0 at it via `DASH0_OTLP_URL=http://127.0.0.1:<port>` (and leave Dash0 SaaS off, or run a dual-pipeline collector). Net effect: prompt-telemetry's analysis layer gets dash0's full 24-event capture surface while data stays on the Mac. The operator-side runbook is at `deploy/dash0-integration.md`.
