# Running MAW on your machine

This guide takes you from a fresh clone to a running MAW with real Gemini/OpenAI models, and to
working on the code with Claude Code. For each integration in depth — the database, LLM providers,
vector stores, MCP servers, A2A agents, tracing, auth — see [INTEGRATIONS.md](INTEGRATIONS.md). Tested on Linux; macOS works the same way. On Windows, use
WSL2 (Ubuntu) and follow the Linux steps inside it.

## 1. Prerequisites

| Tool | Why | Install |
|---|---|---|
| git | clone the repo | your package manager |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Python 3.12 + dependencies | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker (Desktop or Engine) with Compose v2 | Postgres + pgvector, Qdrant, the full stack | docker.com |
| make | the shortcuts below | preinstalled on macOS/Linux (`xcode-select --install` on macOS) |
| Claude Code (optional) | AI pair-programming in this repo | `npm install -g @anthropic-ai/claude-code` |

You do **not** need to install Python yourself: uv downloads 3.12 if your system doesn't have it.

## 2. Clone and set up

```bash
git clone https://github.com/dinup07/maw-api.git
cd maw-api
git checkout claude/festive-feynman-ibsp9e     # until this branch is merged into main
make setup                                      # installs dependencies, creates .env
```

## 3. Add your API keys

Open `.env` (created from `.env.example`; git ignores it) and uncomment the keys you have:

```bash
OPENAI_API_KEY=sk-...            # https://platform.openai.com/api-keys
GOOGLE_API_KEY=AIza...           # https://aistudio.google.com/apikey  (free tier is enough to try)
```

These are API keys, which are billed separately from ChatGPT Plus / Gemini Advanced subscriptions.
Everything except the real-model examples works without any keys.

## 4. Start it

```bash
make services      # Postgres+pgvector (Docker, 127.0.0.1:55432) + mock MCP server + mock A2A agent
make serve         # MAW on http://localhost:8000 — leave it running, use a second terminal below
```

Open <http://localhost:8000/docs> for the interactive API.

## 5. Try it

```bash
make ingest                        # load examples/docs into the keyless `docs` store
make smoke                         # 8 end-to-end checks: catalog workflows, MCP, A2A, RAG

# With your keys: fill the real-embedding stores, then run the real-model checks
make ingest STORE=docs_gemini      # needs GOOGLE_API_KEY
make ingest STORE=docs_openai      # needs OPENAI_API_KEY
REAL=1 make smoke

# Or by hand
curl -s localhost:8000/v1/runs -H 'content-type: application/json' \
  -d "{\"workflow\": $(cat examples/rag_qa_gemini.json), \"input\": \"How many days of annual leave do I get?\"}"
```

More requests: [examples/README.md](../examples/README.md). Writing your own workflows:
[WORKFLOWS.md](WORKFLOWS.md).

## 6. Run the tests

```bash
make test          # fast suite, no services needed (~20 s)
make test-all      # + Postgres/pgvector and Qdrant (starts them in Docker)
make live-test     # real OpenAI/Gemini calls using your .env keys (a few cents)
```

Your `.env` never affects `make test`/`make test-all`: the test suite ignores it, except that the
live tests read the two provider keys from it.

## 7. Working with Claude Code in your IDE

Install Claude Code (see https://docs.claude.com/en/docs/claude-code for the current installer),
then either:

- **VS Code / Cursor / Windsurf:** install the *Claude Code* extension from the marketplace, open the
  `maw-api` folder, and open Claude Code from the editor (or run `claude` in the integrated
  terminal — it connects to the editor so it can see your open files and selections).
- **JetBrains (PyCharm, IntelliJ):** install the *Claude Code* plugin, open the project, and run
  `claude` in the IDE terminal.
- **Plain terminal:** `cd maw-api && claude`. Inside a session, `/ide` connects it to a running IDE.

What the repo already provides for Claude Code:

- `CLAUDE.md` — commands, architecture, rules and gotchas, loaded automatically. You can still run
  `/init`; it will review the codebase and offer to improve the existing file rather than replace it.
- `.claude/settings.json` — pre-approves safe project commands (`make test`, `make lint`,
  `uv run pytest`, …) so you're asked less often, and **denies reading `.env`**, so your API keys
  never enter a conversation. Personal overrides go in `.claude/settings.local.json` (git-ignored).

For the Python tooling in your IDE, select the interpreter at `maw-api/.venv/bin/python` (created by
`make setup`); tests run with pytest from the repo root.

Useful first prompts:

- "Run `make test-all` and `make live-test` and tell me what fails."
- "Add a workflow in `workflows/` that …, validate it and run it against the server."
- "Read docs/DECISIONS.md and explain the trust model for vector stores."

Keep `.env` out of commits (it's in `.gitignore`), and don't paste API keys into chats.

## 8. Run the whole stack in Docker instead

```bash
make up            # builds the image; MAW + Postgres + mocks, http://localhost:8000
make logs
make down
```

Inside Docker, MAW reads your `.env` for keys and settings, but the service URLs (database, mocks,
Qdrant, Jaeger) are fixed to the container names, so host-oriented values in `.env` don't break it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `port is already allocated` on 55432/6333/8000/8001/9001 | Something else uses the port: stop it, or `make stop` |
| `/ready` says `"durable": false` | `MAW_DATABASE_URL` is commented out in `.env`, or `make services` wasn't run |
| RAG run fails with `needs MAW_DATABASE_URL` | Same as above: the internal pgvector store needs Postgres |
| `API key required for Gemini` / `Missing credentials` (OpenAI) | Add the key to `.env` and restart `make serve` |
| `collection … holds … vectors, but the store is configured with …` | You changed a store's embedding model: also change its `collection` and re-ingest |
| `outbound host 'x' is not in MAW_ALLOWED_OUTBOUND_HOSTS` | Add the host to `MAW_ALLOWED_OUTBOUND_HOSTS` in `.env` (JSON list) |
| Gemini model not found | Model names change over time: edit `examples/*gemini*.json` or set `MAW_LIVE_GEMINI_MODEL` |
| Clean slate | `make clean` (deletes local Postgres/Qdrant data) |
