# Implementation decisions

Choices made where the spec is silent or ambiguous. Newest last.

1. **Repository root is the project root.** The spec's §4 tree is rooted at `maw/`; this repo
   (`maw-api`) holds that tree directly at its root rather than in a nested `maw/` folder.
2. **Python 3.12 is pinned via `.python-version`**, to match the `python:3.12-slim` runtime image,
   although `requires-python` stays `>=3.12`.
3. **Reference `input_mapping` maps by node type.** The compat table maps it to `inputs`, but
   native `mcp` and `transform` nodes have no `inputs`: on `mcp_node` it becomes `arguments`, on
   `enrichment_node` it becomes `mapping` (unless one is already present).
4. **Request fields next to `workflow_config` are ignored by `normalize`.** `input_prompt`,
   `session_id`, `async` and `user_id` belong to the run request (handled by `RunRequest`), so a
   reference *request* file also validates as a workflow. Any other stray top-level key is dropped
   with a warning; a top-level `id` is used as the workflow id if `workflow_config` has none.
5. **`{x}` → `{{ x }}` conversion applies to `user_message` only**, as the compat table says.
   Placeholders must look like a path (`[A-Za-z_][A-Za-z0-9_.]*`), so literal JSON in a prompt is
   left alone.
6. **Unsupported reference `config` keys are dropped with a warning** (only `recursion_limit` and
   `max_iterations` have native equivalents).
7. **The function registry and built-ins were built in Phase 1** (the spec schedules them for
   Phase 2) because `maw validate` needs the function names to check references.
8. **The connection registry was built in Phase 2** (scheduled for Phase 4 in the spec), because
   the model registry needs `check_url` and `resolve_secrets`. It makes no network calls.
9. **Path resolution refuses `_`-prefixed attribute names** (e.g. `messages.0.__class__` → None).
   The spec's `getattr` fallback exists for message fields like `content`; blocking private and
   dunder attributes keeps caller-supplied paths from reaching interpreter internals.
10. **Any exception while constructing a provider model becomes `ModelConfigError`**, not just
    `ImportError`/`ValueError`: provider SDKs raise their own types for missing credentials
    (e.g. `openai.OpenAIError`), which are still configuration errors.
11. **`RunService` tracks "active" executions separately from live tasks.** An execution leaves the
    active set just before its final save, so a client that resumes the moment a stream ends (SSE
    or A2A) is not refused with "already running" while the finished task is being cleaned up.
    Subscribers are detached at the same point, so a resume's new subscription can never be closed
    by the previous execution.
12. **Failed runs keep `trace: []`**, as in the spec (§16.4 reads the trace from the final
    snapshot only). Recording the partial trace of a failed run is a candidate improvement.
13. **`EngineContext.mcp`/`a2a` default to `None`** until the connectors land; the `mcp`/`a2a`
    handlers and MCP/agent LLM tools are already written against the spec'd connector API.
14. **The MCP mock annotates tool returns as `dict[str, Any]`.** With mcp 1.30, FastMCP only emits
    `structuredContent` when it can derive an output schema; a bare `dict` annotation yields text
    only. MAW still falls back to parsing JSON text for servers that don't send structured content.
15. **Connector failures get readable, typed errors.** Transport failures inside MCP sessions arrive
    wrapped in anyio `ExceptionGroup`s; MAW unwraps them into `McpToolError("MCP server 'x' call
    failed: …")` / `"… unreachable: …"`. A2A failures become `A2ACallError`, worded "unreachable"
    when an `httpx.TransportError` is in the cause chain and "call failed" otherwise.
16. **A2A data parts are protobuf `Struct`s, so integers come back as floats** (`1` → `1.0`).
    MAW passes them through unchanged rather than guessing which floats were meant as integers.
17. **`build_engine_context` lives in `maw/api/app.py` from Phase 3**, ahead of the rest of the API
    layer, so tests and the future app share one wiring function.
18. **MCP/A2A failures on the connection-discovery endpoints return 502.** The spec's error table
    doesn't cover them; without a mapping they would surface as 500s. Inside a run they still fail
    the run (HTTP 200, `status: failed`).
19. **A mounted A2A agent reads its workflow from the catalog on every request.** Re-registering an
    exposed workflow through the API therefore updates both its behaviour and its agent card (via
    the SDK's `card_modifier` hook) without remounting routes; deleting it makes new tasks fail with
    "no longer available" instead of running a stale definition.
20. **A2A agent URLs are resolved like MCP URLs**, so `${env:…}` (trusted file) and `${secret:…}` work
    in `agents.*.url`. Phase 3 shipped without this; it was caught by the Phase 4 manual smoke test.
21. **`/ready` probes the store with `list_runs(limit=1)`**; the `Store` protocol has no dedicated
    ping, and this exercises the same path real requests use.
22. **The Dockerfile takes `UV_IMAGE` and `PYTHON_IMAGE` build args**, defaulting to the spec's
    `ghcr.io/astral-sh/uv:0.11` and `python:3.12-slim`. Builds behind mirrors or proxies can point
    them elsewhere without editing the file (needed to verify the image in a sandbox that blocks
    ghcr.io). The resulting image is otherwise exactly the spec's.
23. **The MCP mock disables FastMCP's DNS-rebinding protection when bound to a non-loopback
    address.** FastMCP computes that protection at construction time for its default host
    (127.0.0.1), so inside Compose it rejected `Host: mock-mcp:8001` with 421. Found by the Compose
    end-to-end run; the mock stays protected when bound to localhost.
24. **RAG is a seventh node type (`rag`), requested after v1.** Internal store: pgvector inside
    MAW's own Postgres (no extra service); external: Qdrant; plus `memory` for dev/tests. The LLM
    node can also search stores itself (`tools.retrievers`).
25. **Inline vector stores may only be Qdrant.** pgvector reaches a database (MAW's own, or a DSN
    with credentials) and memory is process-wide state, so both are operator decisions made in the
    connections file. Ingestion endpoints only manage trusted stores.
26. **pgvector without fixed dimensions + partial HNSW indexes.** One `maw_vectors` table holds all
    collections in an untyped `vector` column; each collection gets
    `CREATE INDEX … USING hnsw ((embedding::vector(D)) vector_cosine_ops) WHERE collection = …`,
    and searches order by the same expression so the index applies.
27. **Collections are bound to one embedding space.** pgvector and memory record the embedding
    model and dimensions; Qdrant records only the dimensions. Mismatches are refused rather than
    returning meaningless similarities.
28. **Qdrant over REST (httpx), not `qdrant-client`.** Five endpoints are needed; this keeps the
    dependency tree small. Point ids are UUIDv5 of `collection:chunk_id`, so re-ingestion is
    idempotent.
29. **Chunking is built in** (paragraph → sentence → hard split, word-boundary overlap) instead of
    adding `langchain-text-splitters`; it covers plain text and Markdown.
30. **`scripted` embeddings** (hashed bag of words with English stop words removed) make RAG
    runnable and testable without keys. Being keyword hashing, they can produce false matches;
    documented as demo-only.
31. **Example RAG workflows live in `examples/`, not the catalog**, so the spec's catalog of five
    workflows (and its acceptance checks) stays as specified.
32. **`maw` exports `.env` into the process environment** (python-dotenv, existing variables win).
    pydantic-settings only maps `MAW_*` fields, but provider SDKs read `OPENAI_API_KEY` /
    `GOOGLE_API_KEY` from `os.environ`, as do `MAW_SECRET_*` and `${env:…}`. `MAW_ENV_FILE` picks
    another file.
33. **`MAW_ENV=production` fails fast** on development-only settings (no API keys, `*` outbound
    allowlist, inline API keys, no database) instead of warning, because each is exploitable on a
    reachable server.
34. **Tests ignore the developer's configuration:** `conftest.py` drops `MAW_*` variables and points
    `MAW_ENV_FILE` at a missing file; live tests read only the two provider keys from `.env`.
35. **Compose pins container-internal service URLs** rather than interpolating them, because Compose
    also reads the project `.env` for `${…}` and a host-oriented `.env` (127.0.0.1) would break the
    containers.
36. **Real-embedding stores get their own collections** (`docs_gemini`, `docs_openai`) next to the
    keyless `docs`, so switching embedding models never mixes vector spaces. Gemini uses
    `gemini-embedding-001` at 768 dimensions (`text-embedding-004` is being retired).
37. **Single-VM deployment:** Caddy terminates TLS with a `<ip>.sslip.io` hostname (no domain needed),
    publishes only 80/443, hides `/metrics` and disables response buffering for SSE. Postgres gets a
    generated password; secrets live only in `/opt/maw/.env` (0600). Backups are nightly
    `pg_dump`s kept 14 days. The image is built on the VM, so no registry credentials are needed; a
    tag-triggered workflow also publishes it to GHCR.
38. **Remote ingestion goes over the HTTP API** (`scripts/ingest_remote.py`, stdlib only) because the
    image intentionally ships no example documents and Markdown is excluded from the build context.
