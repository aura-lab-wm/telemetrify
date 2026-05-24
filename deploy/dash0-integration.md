# dash0-agent-plugin → telemetrify local OTLP sink

Wire the dash0 plugin to ship every Claude Code event (~24 hook types, not
just `Stop`) into this project's SQLite DB. No Dash0 cloud account needed.

## How it works

```
Claude Code ──hooks──> dash0 binary ──OTLP/HTTP JSON──> FastAPI :8767
                                                            │
                                                            ▼
                                          dash0_spans / dash0_span_events
                                          dash0_log_records / dash0_resources
                                                            │
                                                join on conversation_id
                                                            │
                                                            ▼
                                                       sessions / turns
```

Joins back to your existing `sessions` table via
`dash0_spans.conversation_id == sessions.id` (both are Claude Code's
session UUID).

## One-time setup

### 1. Make sure the UI server is running

```bash
cd ~/Projects/telemetrify
bin/telemetry-ui          # http://127.0.0.1:8767
```

If you're upgrading from an older install, restart the uvicorn process
(`pkill -f telemetry-ui && bin/telemetry-ui`) so the new dash0 routes
load.

### 2. Drop the dash0 config file

Create `~/.claude/dash0-agent-plugin.local.md` (or
`.claude/dash0-agent-plugin.local.md` inside a specific project if you
want per-project capture only):

```markdown
---
otlp_url: "http://127.0.0.1:8767"
auth_token: "local-noop"
dataset: "local"
agent_name: "claude-code"
omit_io: false
omit_user_info: false
---
```

Why each field matters:

- `otlp_url` — base URL the dash0 binary POSTs to. It appends `/v1/traces`,
  `/v1/logs`, `/v1/metrics` automatically (OTLP/HTTP spec).
- `auth_token` — required even though our receiver ignores it. dash0's
  startup check treats empty/missing as "not configured" and silently
  drops telemetry.
- `omit_io: false` — capture full prompts + tool-call arguments + results
  into `dash0_spans.attrs_json`. The whole point of using this locally.
- `omit_user_info: false` — your real `user.name` lands in the resource
  attrs (since this is your own DB, no reason to hash it).

### 3. Reload the plugin

In an active Claude Code session:

```
/reload-plugins
```

Or just start a new session. The hook subprocess reads the config file at
launch.

### 4. Verify

After a few prompts in any project:

```bash
curl -s http://127.0.0.1:8767/dash0/health | jq
```

Expected:

```json
{
  "ok": true,
  "spans_total": 18,
  "logs_total": 3,
  "span_events_total": 4,
  "resources_total": 1,
  "last_span_received_at": "2026-05-24 16:42:11",
  "last_log_received_at": "2026-05-24 16:42:11"
}
```

If `spans_total` is still 0:

1. Confirm dash0 is enabled in `~/.claude/settings.json` under
   `enabledPlugins`: `"dash0@claude-plugins-official": true`.
2. Run Claude Code with `DASH0_DEBUG=true` to see what's being sent
   (it prints OTel payloads to stderr).
3. Confirm the dash0 binary downloaded by checking
   `~/.claude/plugins/data/dash0-claude-plugins-official/bin/`.

## What lands where

| dash0 event class | dash0_* table | conversation_id source |
|---|---|---|
| LLM chat span | `dash0_spans` | `gen_ai.conversation.id` attr |
| Tool invocation span | `dash0_spans` | `gen_ai.conversation.id` attr |
| Permission request/denied | `dash0_spans` | resource attrs |
| Subagent start/stop | `dash0_spans` | parent span context |
| Pre/PostCompact | `dash0_spans` | parent span context |
| Notification | `dash0_log_records` | log attrs |
| Resource fingerprint (service.name, vcs.*, user.*) | `dash0_resources` (deduped) | — |

The full per-event raw payload is **not** stored separately —
`dash0_spans.attrs_json` already carries everything via `omit_io: false`.

## Useful queries

Spans per session:

```sql
SELECT s.id AS session, COUNT(d.span_id) AS spans
FROM sessions s
LEFT JOIN dash0_spans d ON d.conversation_id = s.id
GROUP BY s.id
ORDER BY spans DESC
LIMIT 20;
```

Tool calls and their durations for a session:

```sql
SELECT
  name,
  (end_ns - start_ns) / 1e6 AS duration_ms,
  json_extract(attrs_json, '$."gen_ai.tool.name"') AS tool,
  status_code
FROM dash0_spans
WHERE conversation_id = '<session-uuid>'
  AND name LIKE 'tool.%'
ORDER BY start_ns ASC;
```

Permission denials this week:

```sql
SELECT
  conversation_id,
  datetime(start_ns / 1e9, 'unixepoch') AS at,
  json_extract(attrs_json, '$.tool_name') AS tool
FROM dash0_spans
WHERE name LIKE '%permission%denied%'
  AND start_ns > (unixepoch('now', '-7 days') * 1000000000);
```

## Opting out

**Per-project**: drop `.claude/dash0-agent-plugin.local.md` with
`enabled: false` (or leave the project-level file absent and rely on the
global one — project takes precedence).

**Globally**: delete `~/.claude/dash0-agent-plugin.local.md`, or disable
the plugin entirely:

```
/plugin
```

→ Installed → dash0 → toggle off.

## Caveats

- **HTTPS-only Dash0 cloud + local sink at the same time?** Not possible
  with a single config. Pick one or run a tee proxy (e.g. an
  otel-collector with a fan-out exporter). The local sink alone is
  cheaper and avoids the network round-trip.
- **Protobuf encoding** — our receiver returns 415 for
  `application/x-protobuf`. dash0 currently sends JSON, so this is a
  no-op. If a future dash0 release switches to protobuf, install
  `opentelemetry-proto` and the receiver fix is one-line.
- **No deduplication on logs.** Spans dedup on `(trace_id, span_id)`;
  log records insert every time. If you POST the same export twice you
  get duplicate log rows. Spans never duplicate.
- **The DB will grow.** A typical heavy session is ~150 spans + ~20 log
  records. After a month of moderate use plan for an extra ~50 MB
  beyond the existing `data/prompts.db` growth.

## Where the code lives

- Receiver:     `src/telemetrify/dash0/receiver.py`
- Store:        `src/telemetrify/dash0/store.py`
- Schema:       `src/telemetrify/migrations/016_dash0_otel.sql`
- Tests:        `tests/dash0/`
- This doc:     `deploy/dash0-integration.md`
- Comparison vs vanilla dash0: `deploy/comparison-with-dash0.md`
