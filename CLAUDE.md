# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

MAW is a runtime that executes multi-agent workflows defined as JSON. A definition is validated,
compiled into a LangGraph `StateGraph` (cached by content hash) and run. Node types: `llm`, `mcp`,
`a2a`, `function`, `transform`, `human`, `rag`. Runs are checkpointed in Postgres (durable,
resumable, crash-recoverable), any workflow can be exposed as an A2A agent, and everything runs
without API keys via the `scripted` model and the mock services in `mocks/`.

The original build spec is the source of truth for v1 behaviour; `docs/DECISIONS.md` records every
place the implementation chose or deviated, and why. Read it before changing behaviour.

## Commands

```bash
make setup        # uv sync + create .env from .env.example
make services     # Postgres+pgvector in Docker (127.0.0.1:55432) + mock MCP (:8001) + mock A2A (:9001)
make serve        # uv run maw serve --reload   -> http://localhost:8000/docs
make ingest       # load examples/docs into the `docs` vector store (STORE=docs_gemini|docs_openai)
make smoke        # end-to-end checks against a running server (REAL=1 adds Gemini/OpenAI checks)
make test         # fast suite; DB/Qdrant/live tests skip
make test-all     # + Postgres and Qdrant (starts them); live tests still skip without keys
make live-test    # real OpenAI/Gemini calls (keys from .env; costs cents)
make lint         # ruff check + format check + maw validate  (run before every commit)
make fmt          # auto-fix
make up / down    # whole stack in Docker
```

Single test: `uv run pytest tests/test_engine.py::test_human_interrupt_and_resume -q`.
CLI: `uv run maw validate FILE...`, `uv run maw schema`, `uv run maw ingest STORE FILE...`.

## Layout and the dependency rule

```
src/maw/
  schema/        workflow contract (Pydantic, extra="forbid"), reference-format compat, validate_graph
  engine/        state + reducers, paths/templates, routing, models (+ ScriptedChatModel), compiler,
                 nodes/ (base.py wrapper, llm.py, rag.py, handlers.py for mcp/a2a/function/transform/human)
  connectors/    registry (secrets, trust, allowlist), mcp_client, a2a_client, embeddings, vectorstores
  functions/     local function registry + builtins (calculator is AST-whitelisted, never eval)
  runtime/       RunRecord + stores (memory/Postgres), catalog, persistence, service.py (RunService)
  a2a_server/    agent cards, mounting (/a2a/<id>), WorkflowAgentExecutor
  api/           FastAPI app + lifespan, auth middleware, error mapping, routers
  observability/ JSON logging with run/node ids, Prometheus metrics, OTel spans
```

`api → runtime → engine → connectors/functions/schema → observability/settings`; `a2a_server →
runtime`. Lower layers never import upper ones; nothing in `src/` imports `mocks/`.

## Rules that matter

- **One execution path.** REST sync/async, SSE and the A2A server all go through `RunService`. Never
  run a graph anywhere else.
- **Every state key has a reducer** (`engine/state.py`); parallel branches write the same keys.
- **Node wrappers re-raise `GraphBubbleUp` and `asyncio.CancelledError`** — interrupts and
  cancellation are control flow, not errors.
- **Trust model.** The connections file (`config/connections.yaml`) is trusted; anything in a
  submitted workflow is untrusted. Untrusted values may only use `${secret:NAME}` (→ `MAW_SECRET_NAME`),
  never `${env:...}`; stdio MCP servers and pgvector/memory vector stores only come from the file;
  every outbound URL (MCP, A2A *and the URLs inside agent cards*, model/embedding `base_url`,
  Qdrant) goes through `ConnectionRegistry.check_url`. Secrets are resolved per call, never cached
  except as hashes in cache keys, never logged, never returned by `/v1/connections`.
- **Bare strings in mappings are paths**; literals are quoted (`"'text'"`).
- **A vector collection holds one embedding model.** Changing a store's embedding means a new
  `collection`.
- **Postgres via psycopg 3:** one statement per `execute()`; pools use `autocommit=True,
  prepare_threshold=0, row_factory=dict_row`; parameterized queries only.
- **Pins:** `mcp<2` (langchain-mcp-adapters 0.3 breaks on mcp 2.x). The A2A SDK's
  `create_rest_routes` adds a root `Mount('/{tenant}')` that must be filtered out (see
  `a2a_server/mount.py`). Details: `docs/ARCHITECTURE.md` → Pin notes.
- **No blocking I/O in async code** (ruff `ASYNC` rules are on).
- Metric labels stay low-cardinality (no run ids, no user input).

## Adding things

- **Node type:** schema model in `schema/workflow.py` (+ the `Node` union), handler in
  `engine/nodes/`, entry in `HANDLERS`, reference checks in `validate_graph`, tests, docs in
  `docs/WORKFLOWS.md`.
- **Vector-store backend:** implement the `Backend` protocol in `connectors/vectorstores.py`, add the
  provider literal to `VectorStoreSpec`, add it to the parametrized `backend` fixture in
  `tests/test_rag.py`.
- **Local function:** `@register_function("name")` in a module listed in `MAW_FUNCTION_MODULES`.

## Tests

`tests/conftest.py` strips `MAW_*` variables and ignores `.env`, so a developer `.env` never leaks into
tests. Mocks run as real subprocesses; API tests run a real uvicorn server in a thread
(`ServerThread`). Opt-in suites: `MAW_TEST_DATABASE_URL` (durability, pgvector), `MAW_TEST_QDRANT_URL`
(Qdrant), provider keys (`-m live`). Never weaken or skip a test to make it pass; fix the cause.

## Configuration

Settings are `MAW_*` env vars (see `src/maw/settings.py` and `.env.example`). Never read or print
`.env` (it holds the developer's API keys; `.claude/settings.json` denies it). `maw` loads `.env` into
the process environment, so provider keys there reach LangChain. `MAW_ENV=production` refuses to start
without API keys, an outbound allowlist and a database. Deployment: `docs/DEPLOY.md`.

## Docs to keep in sync

`README.md`, `docs/LOCAL_SETUP.md`, `docs/INTEGRATIONS.md` (per-integration setup), `docs/DEPLOY.md`,
`docs/WORKFLOWS.md` (authoring reference), `docs/API.md`, `docs/ARCHITECTURE.md`,
`docs/DECISIONS.md` (append a numbered entry for any non-obvious choice), `examples/README.md`.
