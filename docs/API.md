# MAW HTTP API

Base URL: `http://localhost:8000` by default. Interactive OpenAPI docs are at `/docs`.

## Authentication

When `MAW_API_KEYS` is set (a JSON list), every request needs `X-API-Key: <key>` or
`Authorization: Bearer <key>`. Public without a key: `/health`, `/ready`, `/metrics`, `/docs`,
`/redoc`, `/openapi.json`, and every `…/.well-known/agent-card.json`. Otherwise the response is
`401 {"detail": "missing or invalid API key"}` with `WWW-Authenticate: Bearer`.

## Errors

| Status | When | Body |
|---|---|---|
| 422 | Invalid workflow (schema or graph), or no/both of `workflow` and `workflow_id` | `{"detail": "invalid workflow", "errors": [...]}` |
| 404 | Unknown run, workflow or connection | `{"detail": "..."}` |
| 409 | Run in the wrong state (resume a non-paused run, cancel a finished one) | `{"detail": "..."}` |
| 400 | Bad connection or model configuration (e.g. host not allowlisted) | `{"detail": "..."}` |
| 502 | An MCP server or A2A agent failed on a connection-discovery endpoint | `{"detail": "..."}` |
| 403 | Workflow registration disabled | `{"detail": "..."}` |

Failures **during** a run (a node error, an unreachable agent) are not HTTP errors: the response is
200 with `"status": "failed"` and an `error` object `{"type", "message", "node"?}`.

## Health

| Endpoint | Response |
|---|---|
| `GET /health` | `{"status": "ok", "version": "0.1.0"}` |
| `GET /ready` | `{"status": "ready", "durable": true, "workflows": 5, "graph_cache": {"size", "capacity"}}`, or 503 `{"status": "unavailable", "error"}` if the store is unreachable |
| `GET /metrics` | Prometheus text format |

## Runs

### `POST /v1/runs` — start a run

```json
{ "workflow_id": "support_triage", "input": "Was INV-1001 charged twice?", "thread_id": null, "mode": "sync" }
```

| Field | Notes |
|---|---|
| `workflow` / `workflow_id` | Exactly one: an inline definition (native or reference format) or a catalog id |
| `input` | String or any JSON value |
| `thread_id` | Reuse to continue a conversation; a new `thr_…` id is created otherwise |
| `mode` | `sync` (default, 200 when finished) or `async` (202 immediately; poll `GET /v1/runs/{id}`) |

The reference request shape is also accepted: `{"workflow_config": {...}, "config": {...},
"input_prompt": "...", "session_id": "...", "async": "true" | "false", "user_id": "..."}`.

Response (`RunResponse`):

```json
{
  "id": "run_…", "workflow_id": "support_triage", "workflow_hash": "…", "thread_id": "thr_…",
  "status": "completed",
  "output": { "text": "INV-1001 was charged twice. A refund of $129.00 …", "category": "billing", "urgency": "normal",
              "billing_details": { "invoice": "INV-1001", "found": true, "action": "refund_issued", "amount": 129.0 } },
  "text": "INV-1001 was charged twice. …",
  "error": null, "interrupt": null, "errors": [], "warnings": [],
  "trace": [
    { "node": "classify", "type": "llm", "ms": 3.1, "status": "ok", "model": "scripted:triage-router", "tool_calls": 0 },
    { "node": "billing", "type": "a2a", "ms": 40.2, "status": "ok", "agent": "billing", "remote_state": "completed" },
    { "node": "respond", "type": "transform", "ms": 0.1, "status": "ok" }
  ],
  "duration_ms": 52.4, "created_at": "…", "updated_at": "…"
}
```

`status` is one of `queued`, `running`, `interrupted`, `completed`, `failed`, `cancelled`. `text` is
`output.text` (or `output` if it's a string). `interrupt` lists the pause prompts
(`[{"node", "prompt"}]`) when the run is `interrupted`.

### `POST /v1/runs/stream` — start a run and stream events (SSE)

Same body as `POST /v1/runs`. The response is `text/event-stream`; each event is

```
event: node_completed
data: {"event": "node_completed", "run_id": "run_…", "thread_id": "thr_…", "data": {"node": "search", "status": "ok", "ms": 12.3, "result": {...}}}
```

Sequence: `run_created` (`data.warnings`) → `run_started` → `node_completed`* → one of
`run_completed` (`data.output`), `run_failed` (`data.error`), `interrupted` (`data.interrupt`),
`run_cancelled`.

### Other run endpoints

| Endpoint | Notes |
|---|---|
| `GET /v1/runs?status=&workflow_id=&limit=50` | Newest first; `limit` 1–500 |
| `GET /v1/runs/{id}` | 404 if unknown |
| `POST /v1/runs/{id}/cancel` | Cancels a queued, running or interrupted run; 409 if already finished |
| `POST /v1/runs/{id}/resume` | Body `{"value": any, "mode": "sync" \| "async"}`; only for `interrupted` runs (else 409) |
| `POST /v1/runs/{id}/resume/stream` | Same, streaming; the first event is `run_resumed` |

## Workflows

| Endpoint | Notes |
|---|---|
| `GET /v1/schema` | JSON Schema of the workflow format (title `WorkflowDefinition`) |
| `POST /v1/workflows/validate` | Body: a definition. Always 200: `{"valid": true, "errors": [], "warnings", "hash", "normalized", "mermaid"}` or `{"valid": false, "errors", "warnings": []}` |
| `GET /v1/workflows` | `[{"id", "hash", "source": "file" \| "api", "description", "node_count", "a2a_url"}]` |
| `GET /v1/workflows/{id}` | `{"summary", "definition", "mermaid"}` |
| `POST /v1/workflows` | Body `{"workflow": {...}}` → 201 summary. Exposed workflows are mounted as A2A agents immediately. 422 if invalid or if the id belongs to a file workflow; 403 if registration is disabled |
| `DELETE /v1/workflows/{id}` | 204. 404 if unknown, 422 for file workflows |

## Connections

| Endpoint | Notes |
|---|---|
| `GET /v1/connections` | Server-side connections without headers or env: `{"mcp_servers": {name: {transport, url, command}}, "agents": {name: {url, description}}}` |
| `GET /v1/connections/mcp/{name}/tools` | `[{"name", "description", "input_schema"}]` |
| `GET /v1/connections/agents/{name}/card` | The agent card as JSON |

## A2A agents

Each workflow with `"expose": {"a2a": true}` is an A2A 1.0 agent:

| Endpoint | Purpose |
|---|---|
| `GET /a2a/<id>/.well-known/agent-card.json` | Agent card (JSON-RPC and HTTP+JSON interfaces) |
| `POST /a2a/<id>` | JSON-RPC binding (`SendMessage`, `SendStreamingMessage`, `GetTask`, `CancelTask`, …) |
| `/a2a/<id>/message:send`, `/a2a/<id>/message:stream`, `/a2a/<id>/tasks/{id}`, … | HTTP+JSON binding |

Use any A2A 1.0 client. See `docs/WORKFLOWS.md` → *Exposing as an A2A agent* for how messages map
to runs.
