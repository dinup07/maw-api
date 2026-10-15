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
