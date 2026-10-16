# MAW — Multi-Agent Workflow runtime

MAW runs multi-agent workflows that are defined as JSON. Each definition is validated, compiled into
a [LangGraph](https://github.com/langchain-ai/langgraph) `StateGraph` (cached by content hash) and
executed. Nodes can:

- call an **LLM** (any LangChain provider), optionally with tools from **MCP servers**, **A2A
  agents** or **local Python functions**, and optionally return **structured JSON**;
- call one **MCP tool** deterministically;
- call an external **A2A agent** (Agent-to-Agent protocol 1.0);
- **retrieve context** from a vector store (**RAG**): the internal pgvector store in MAW's own
  Postgres, or an external Qdrant;
- run a **local function**, **transform** state, or **pause for a human** and resume later.

Edges can be conditional, fan out in parallel, join, and loop (bounded). Runs are checkpointed in
Postgres, so they can be async, cancelled, resumed after a human pause, and **recovered after a
crash**. Any workflow can be **exposed as an A2A agent**. Traces go to any OTLP collector
(Jaeger, Langfuse), metrics to Prometheus, and logs are JSON.

The core is domain-neutral: external systems are reached through MCP servers, A2A agents and LLM
providers configured as data. Everything runs **without API keys**, using a deterministic
`scripted` model and bundled mock services.

## Quick start

### Docker Compose (MAW + Postgres + mock MCP server + mock A2A agent)

```bash
docker compose up --build -d
curl -s localhost:8000/ready
curl -s localhost:8000/v1/runs -H 'content-type: application/json' \
  -d '{"workflow_id": "support_triage", "input": "Was INV-1001 charged twice?"}'
```

Add `--profile tracing` and `MAW_TRACING_ENABLED=true` to send traces to Jaeger
(UI at <http://localhost:16686>).

### Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python mocks/mcp_kb_server.py --host 127.0.0.1 --port 8001 &
uv run python mocks/a2a_billing_agent.py --host 127.0.0.1 --port 9001 &
uv run maw serve            # http://localhost:8000, OpenAPI docs at /docs
```

Without `MAW_DATABASE_URL`, runs and checkpoints are kept in memory and lost on restart.

### CLI

```bash
uv run maw validate workflows/*.json   # parse + validate; exits 1 on any failure
uv run maw schema                      # JSON Schema of the workflow format
uv run maw serve [--host H] [--port P] [--reload]
```

## A workflow

```json
{
  "id": "hello",
  "nodes": [
    { "id": "reply", "type": "llm", "model": "scripted", "system_prompt": "You are a friendly assistant." }
  ],
  "edges": [
    { "source": "start", "target": "reply" },
    { "source": "reply", "target": "end" }
  ],
  "expose": { "a2a": true }
}
```

Put workflow files in `workflows/` (loaded at startup; the id defaults to the file name), register
them with `POST /v1/workflows`, or send them inline with each run. The catalog ships five examples:

| Workflow | Shows |
|---|---|
| `hello` | One LLM node, exposed as an A2A agent |
| `support_triage` | Structured classification and conditional routing to an A2A agent, an LLM with MCP tools, a deterministic MCP lookup, or a ticket (with `on_error: continue`) |
| `kb_research` | Parallel fan-out (MCP search + local function) and a deferred join |
| `approval` | Human-in-the-loop with a bounded revision loop |
| `concierge` | One LLM that uses MCP tools and an A2A agent as tools |

### Retrieval (RAG)

```bash
uv run maw ingest docs examples/docs/*.md           # needs MAW_DATABASE_URL (internal pgvector store)
curl -s localhost:8000/v1/runs -H 'content-type: application/json' \
  -d "{\"workflow\": $(cat examples/rag_qa.json), \"input\": \"how many days of annual leave do I get\"}"
```

`examples/rag_qa_gemini.json` does the same with Gemini writing the answer from the retrieved
passages. Configure stores (and real embedding models) under `vector_stores` in
`config/connections.yaml`.

See [docs/WORKFLOWS.md](docs/WORKFLOWS.md) for the authoring reference,
[docs/API.md](docs/API.md) for the HTTP API, [examples/README.md](examples/README.md) for a curl
walkthrough, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Configuration

All settings are environment variables prefixed `MAW_` (or a `.env` file). See
[.env.example](.env.example) for the full list. The most important:

| Variable | Default | Purpose |
|---|---|---|
| `MAW_DATABASE_URL` | unset (in-memory) | `postgresql://…` enables durable runs and checkpoints |
| `MAW_PUBLIC_URL` | `http://localhost:8000` | Base URL used in A2A agent cards |
| `MAW_API_KEYS` | `[]` | JSON list; non-empty enables API-key auth |
| `MAW_ALLOWED_OUTBOUND_HOSTS` | `["*"]` | Glob allowlist for MCP, A2A and model `base_url` hosts |
| `MAW_CONNECTIONS_FILE` | `config/connections.yaml` | Trusted, server-side MCP servers and A2A agents |
| `MAW_WORKFLOWS_DIR` | `workflows` | Catalog directory |
| `MAW_TRACING_ENABLED` | `false` | Export OTLP traces (`OTEL_EXPORTER_OTLP_*`) |

LLM provider keys use the provider's usual variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …).
Workflows reference other secrets as `${secret:NAME}`, which reads `MAW_SECRET_NAME`.

## Security checklist for shared deployments

- **Set `MAW_ALLOWED_OUTBOUND_HOSTS`.** The default `*` is for development only; without an
  allowlist, a submitted workflow can make MAW call any host (SSRF).
- **Set `MAW_API_KEYS`.** Health probes, metrics, docs and A2A agent cards stay public.
- Keep `MAW_ALLOW_INLINE_API_KEYS=false` so keys are never pasted into workflow JSON.
- Consider `MAW_ALLOW_WORKFLOW_REGISTRATION=false` if workflows should only come from files.
- Submitted workflows can only read `MAW_SECRET_*` variables, and can't define `stdio` MCP servers
  (those run local commands); only the server-side connections file can.

## Development

```bash
uv run ruff check src tests mocks
uv run pytest -q                       # durability tests skip without a database
MAW_TEST_DATABASE_URL=postgresql://maw:maw@localhost:5432/maw uv run pytest -q
```

Tests start the mock MCP server and mock A2A agent as subprocesses, and the API tests run a real
uvicorn server.
