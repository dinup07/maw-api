# Setting up integrations locally

[LOCAL_SETUP.md](LOCAL_SETUP.md) gets MAW running. This guide explains each integration: what it
is for, how to turn it on locally, how to check it works, and what to do when it doesn't. Every
integration is optional — MAW runs end to end with none of them.

| Integration | Needed for | Configured in |
|---|---|---|
| [Postgres (database)](#1-postgres-database) | durable runs, crash recovery, the internal vector store | `.env` → `MAW_DATABASE_URL` |
| [LLM providers](#2-llm-providers-openai-gemini-and-others) | real models in `llm` nodes | `.env` keys + workflow `models` |
| [Vector stores and embeddings](#3-vector-stores-and-embeddings-rag) | `rag` nodes, LLM retriever tools | `config/connections.yaml` → `vector_stores` |
| [MCP servers](#4-mcp-servers-tools) | `mcp` nodes, MCP tools for LLMs | `config/connections.yaml` → `mcp_servers` |
| [A2A agents](#5-a2a-agents) | `a2a` nodes, agents as LLM tools, exposing workflows | `connections.yaml` → `agents`; workflow `expose` |
| [Tracing and metrics](#6-tracing-and-metrics) | seeing what a run did | `.env` → `MAW_TRACING_ENABLED`, `OTEL_*` |
| [API keys and the allowlist](#7-api-keys-and-the-outbound-allowlist) | locking MAW down | `.env` → `MAW_API_KEYS`, `MAW_ALLOWED_OUTBOUND_HOSTS` |

Two files hold all configuration:

- **`.env`** (git-ignored, created by `make setup`): secrets and per-machine settings. `maw` loads
  it into the environment at startup; variables already set in your shell win. Restart `make serve`
  after editing it.
- **`config/connections.yaml`** (committed): the *trusted*, server-side list of MCP servers, A2A
  agents and vector stores that workflows reference by name. Values can use `${env:NAME}`,
  `${env:NAME:-default}` and `${secret:NAME}` (reads `MAW_SECRET_NAME`). Restart MAW after editing.

Workflows can also define their own MCP servers, agents and (Qdrant) vector stores inline, but those
are *untrusted*: they may only read `${secret:…}`, can't run local commands, and every URL must pass
the outbound allowlist.

---

## 1. Postgres (database)

**What it gives you.** Without a database MAW keeps runs and checkpoints in memory: fine for
trying things, but everything is lost on restart. With Postgres:

- run records and every LangGraph checkpoint are stored, so `GET /v1/runs/{id}` works after a
  restart, paused (human-in-the-loop) runs can be resumed days later, and runs interrupted by a
  crash continue from their last completed step when MAW starts again;
- workflows registered with `POST /v1/workflows` survive restarts;
- the **internal vector store** (pgvector) lives in the same database.

### Option A — Docker (recommended)

```bash
make db            # Postgres 16 + pgvector, published on 127.0.0.1:55432, data in a Docker volume
```

`.env` already points at it:

```bash
MAW_DATABASE_URL=postgresql://maw:maw@127.0.0.1:55432/maw
```

MAW creates its tables on startup; there are no migrations to run.

### Option B — your own Postgres

Any Postgres 14+ works for runs and checkpoints. For the internal vector store it also needs the
**pgvector** extension (`apt install postgresql-16-pgvector`, `brew install pgvector`, or a managed
Postgres that offers it — Supabase, Neon, RDS, Cloud SQL all do).

```sql
CREATE USER maw WITH PASSWORD 'choose-one';
CREATE DATABASE maw OWNER maw;
\c maw
CREATE EXTENSION IF NOT EXISTS vector;   -- needs a superuser once; MAW then uses it as `maw`
```

```bash
MAW_DATABASE_URL=postgresql://maw:choose-one@localhost:5432/maw
```

Use `?sslmode=require` on the URL for hosted databases. MAW disables prepared statements, which
connection poolers such as PgBouncer need.

### Check it

```bash
curl -s localhost:8000/ready          # "durable": true
psql postgresql://maw:maw@127.0.0.1:55432/maw -c '\dt'
```

Tables MAW creates:

| Table | Contents |
|---|---|
| `maw_runs` | one row per run: status, input, output, trace, error, and the workflow definition it ran |
| `maw_workflows` | workflows registered through the API |
| `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` | LangGraph state after every step (what makes resume and crash recovery work) |
| `maw_vector_collections`, `maw_vectors` | the internal vector store (created on first use) |

Useful queries:

```sql
SELECT id, workflow_id, status, updated_at FROM maw_runs ORDER BY created_at DESC LIMIT 10;
SELECT status, count(*) FROM maw_runs GROUP BY status;
SELECT name, embedding, dimensions FROM maw_vector_collections;
SELECT collection, count(*) FROM maw_vectors GROUP BY collection;
```

To browse it from your IDE's database tool: host `127.0.0.1`, port `55432`, database/user/password
`maw`.

### Try durability

```bash
# start a run that pauses for approval
curl -s localhost:8000/v1/runs -H 'content-type: application/json' -d '{"workflow_id": "approval", "input": "refund"}'
# stop `make serve` (Ctrl-C), start it again, then resume with the run id from above:
curl -s localhost:8000/v1/runs/<run_id>/resume -H 'content-type: application/json' -d '{"value": "approve"}'
```

### Reset

`make clean` deletes the Docker volume (all runs, checkpoints and vectors). To keep the container
but empty MAW's data: `psql … -c 'DROP TABLE IF EXISTS maw_runs, maw_workflows, maw_vectors, maw_vector_collections, checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations CASCADE'`.

---

## 2. LLM providers (OpenAI, Gemini and others)

**What it gives you.** Real models in `llm` nodes instead of the deterministic `scripted` model.

### Keys

Put the keys in `.env` (they are API keys, billed per use — not ChatGPT/Gemini app subscriptions):

```bash
OPENAI_API_KEY=sk-...        # https://platform.openai.com/api-keys
GOOGLE_API_KEY=AIza...       # https://aistudio.google.com/apikey  (free tier available)
# ANTHROPIC_API_KEY=sk-ant-...
```

### Using a model in a workflow

```json
"models": {
  "gemini": { "provider": "google_genai", "model": "gemini-2.5-flash", "temperature": 0 },
  "gpt":    { "provider": "openai",       "model": "gpt-4o-mini" },
  "claude": { "provider": "anthropic",    "model": "<a current Claude model id>" }
},
"nodes": [ { "id": "answer", "type": "llm", "model": "gemini", "system_prompt": "..." } ]
```

Shorthand without a `models` entry: `"model": "openai:gpt-4o-mini"` or
`"model": "google_genai:gemini-2.5-flash"`.

Installed provider packages: OpenAI, Anthropic and Gemini. Others work after adding their LangChain
package — e.g. local models with Ollama:

```bash
uv add langchain-ollama
ollama pull llama3.2
```
```json
"local": { "provider": "ollama", "model": "llama3.2", "base_url": "http://localhost:11434" }
```

OpenAI-compatible servers (vLLM, LM Studio, LiteLLM, Groq's OpenAI endpoint…) use
`"provider": "openai"` with a `base_url`; put their key in `.env` as `MAW_SECRET_<NAME>` and
reference it as `"api_key": "${secret:<NAME>}"` (literal keys in workflow JSON are refused).

### Check it

```bash
make live-test                                # plain answer, structured output, tool loop, RAG, triage
curl -s localhost:8000/v1/runs -H 'content-type: application/json' \
  -d "{\"workflow\": $(cat examples/gemini_support_triage.json), \"input\": \"my vpn password expired\"}"
```

The run's `trace` shows the model label (`google_genai:gemini-2.5-flash`) and the number of tool
calls per `llm` node.

| Error | Cause |
|---|---|
| `ModelConfigError: … API key required` / `Missing credentials` | key missing from `.env`, or `make serve` not restarted |
| `404 … model not found` | model names change: use a current one from the provider's docs |
| `output did not match output_schema` | the model returned the wrong shape; use a stronger model, or `recover_text_field` |

---

## 3. Vector stores and embeddings (RAG)

**What it gives you.** `rag` nodes and `tools.retrievers` search your documents by meaning and feed
the best passages to the next node.

Each store in `config/connections.yaml` has a backend and an embedding model:

| Store (already configured) | Backend | Embedding | Needs |
|---|---|---|---|
| `docs` | internal pgvector | `scripted` (keyword hashing, keyless, demo only) | Postgres |
| `docs_gemini` | internal pgvector | `models/gemini-embedding-001` (768 dims) | Postgres + `GOOGLE_API_KEY` |
| `docs_openai` | internal pgvector | `text-embedding-3-small` | Postgres + `OPENAI_API_KEY` |

### Load documents and search

```bash
make ingest STORE=docs_gemini                         # examples/docs/*.md
uv run maw ingest docs_gemini ~/company-handbook/*.md # your own .txt/.md/.rst files
curl -s localhost:8000/v1/vector-stores/docs_gemini/query -H 'content-type: application/json' \
  -d '{"query": "how much time off do I get?", "top_k": 3}'
```

Re-ingesting a file replaces it (ids are file paths). Then run `examples/rag_qa_gemini.json` or
`examples/rag_qa_openai.json`.

### Add an external store (Qdrant)

```bash
make qdrant                    # Qdrant on http://127.0.0.1:6333 (dashboard at /dashboard)
```

```yaml
# config/connections.yaml → vector_stores:
  research:
    provider: qdrant
    url: ${env:QDRANT_URL:-http://localhost:6333}
    collection: research
    embedding: {provider: google_genai, model: models/gemini-embedding-001, params: {output_dimensionality: 768}}
    # Qdrant Cloud: add  api_key: ${secret:QDRANT_API_KEY}  and set MAW_SECRET_QDRANT_API_KEY in .env
```

### Use a different Postgres for vectors

```yaml
  team_db:
    provider: pgvector
    url: ${env:TEAM_PG_DSN}        # postgresql://user:pass@host:5432/db (needs the vector extension)
    collection: handbook
    embedding: {provider: openai, model: text-embedding-3-small}
```

**Rule:** a collection holds vectors from one embedding model. To change a store's model, also
change its `collection` and re-ingest — MAW refuses to mix them.

---

## 4. MCP servers (tools)

**What it gives you.** `mcp` nodes call one tool deterministically; `llm` nodes with
`"tools": {"mcp": ["name"]}` let the model choose tools. Locally, `make mocks` runs a demo
knowledge-base server (`kb`: `search_kb`, `create_ticket`, `get_ticket`) on port 8001.

Add real servers to `config/connections.yaml`:

```yaml
mcp_servers:
  # Remote server over HTTP (streamable_http is the default; "sse" for older servers)
  github:
    url: https://api.githubcopilot.com/mcp/
    headers:
      Authorization: Bearer ${secret:GITHUB_TOKEN}     # MAW_SECRET_GITHUB_TOKEN=ghp_... in .env
  # Local process over stdio (only allowed here, never inside a submitted workflow)
  filesystem:
    transport: stdio
    command: npx                                      # needs Node.js
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/notes"]
```

### Check it

```bash
curl -s localhost:8000/v1/connections                       # lists configured servers (no secrets)
curl -s localhost:8000/v1/connections/mcp/github/tools      # discovers the server's tools
```

Use `allow`/`block` to limit what an LLM sees:
`"tools": {"mcp": [{"server": "github", "allow": ["search_*", "get_*"]}]}`.

---

## 5. A2A agents

**Calling agents.** `a2a` nodes send a message to any A2A 1.0 agent; `"tools": {"agents": ["name"]}`
lets an LLM decide when to ask it. Locally, `make mocks` runs a demo billing agent on port 9001.

```yaml
agents:
  billing:
    url: ${env:BILLING_AGENT_URL:-http://localhost:9001}   # card fetched from <url>/.well-known/agent-card.json
  partner:
    url: https://agents.partner.example
    headers: {Authorization: Bearer ${secret:PARTNER_TOKEN}}
    description: Used as the tool description when an LLM can call it.
```

Check: `curl -s localhost:8000/v1/connections/agents/billing/card`.

**Exposing your workflows as agents.** Add `"expose": {"a2a": true}` to a workflow (the catalog's
`hello`, `support_triage` and `concierge` already have it). It is then served at
`http://localhost:8000/a2a/<id>` with its card at `/a2a/<id>/.well-known/agent-card.json`, usable from
any A2A client — including another MAW workflow (see `examples/README.md` → *A2A*). Set
`MAW_PUBLIC_URL` to the address other agents use to reach you; it is written into the card.

---

## 6. Tracing and metrics

**Traces** show each run as a tree: `workflow.run` → `node.<id>` → `llm.invoke` / `mcp.call_tool` /
`a2a.send_message` / `vector.search`.

Jaeger locally:

```bash
docker compose --profile tracing up -d jaeger     # UI: http://localhost:16686
```
```bash
# .env
MAW_TRACING_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Restart `make serve`, run a workflow, then pick service `maw` in the Jaeger UI.

Langfuse (LLM-focused tracing, cloud or self-hosted):

```bash
MAW_TRACING_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 of public_key:secret_key>
```

**Metrics:** `curl -s localhost:8000/metrics` (Prometheus format: runs, node durations, LLM calls and
tokens, MCP/A2A/vector calls, graph cache).

**Logs:** `MAW_LOG_JSON=false` gives readable logs with `[run=… node=…]` prefixes; JSON is the
default for production.

---

## 7. API keys and the outbound allowlist

Locally both are off. To try production-like settings:

```bash
MAW_API_KEYS=["dev-key-1"]          # then send  X-API-Key: dev-key-1  (or Authorization: Bearer …)
MAW_ALLOWED_OUTBOUND_HOSTS=["localhost","127.0.0.1","api.openai.com","generativelanguage.googleapis.com"]
```

With `MAW_API_KEYS` set, everything except `/health`, `/ready`, `/metrics`, `/docs` and A2A agent
cards needs a key (`./scripts/smoke.sh` reads it from `API_KEY=`). With an allowlist, every MCP, A2A,
model, embedding and Qdrant host must match a pattern (`*.example.com` globs work), including the
URLs inside A2A agent cards. `MAW_ENV=production` refuses to start without both.

---

## Ports used locally

| Port | Service | Started by |
|---|---|---|
| 8000 | MAW | `make serve` |
| 8001 | mock MCP server | `make mocks` / `make services` |
| 9001 | mock A2A agent | `make mocks` / `make services` |
| 55432 | Postgres + pgvector | `make db` / `make services` |
| 6333 | Qdrant | `make qdrant` |
| 16686, 4318 | Jaeger UI, OTLP | `docker compose --profile tracing up -d jaeger` |
