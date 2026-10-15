# MAW architecture

> This document grows phase by phase. The full architecture write-up (layers, run lifecycle,
> security model) lands in Phase 5 of the build; the pin notes below are maintained from Phase 0.

## Layers and dependency rule

`api → runtime → engine → connectors/functions/schema → observability/settings`.
`a2a_server` depends on `runtime`. Lower layers never import upper layers, and nothing under
`src/` imports `mocks/`.

## Pin notes

Verified on 2026-10-15 against the installed versions (Python 3.12.3):

| Package | Version |
|---|---|
| langgraph | 1.2.15 |
| langchain | 1.4.6 (langchain-core 1.6.10) |
| langchain-mcp-adapters | 0.3.2 |
| mcp | 1.30.0 |
| a2a-sdk | 1.2.2 |
| fastapi | 0.143.2 (starlette 1.8.0) |
| pydantic | 2.14.1 |
| langgraph-checkpoint-postgres | 3.2.0 (psycopg 3.3.6) |

Findings, keyed to the spec's trap list (§3.3):

- **T1 — `mcp` is pinned `<2`.** `langchain-mcp-adapters` 0.3.x declares `mcp>=1.24` with no upper
  bound but fails at import on mcp 2.x. The resolver picked mcp 1.30.0 under our pin and the
  adapter imports cleanly. The MCP wire protocol negotiates versions, so a 1.x client still
  talks to servers built on SDK 2.x.
- **T2 — the 1.x server class is `mcp.server.fastmcp.FastMCP`.** Its `settings` expose `host`,
  `port` and `streamable_http_path` (default `/mcp`).
- **T3/T4 — adapter.** `MultiServerMCPClient` has `session(server_name)` (raw session for
  deterministic calls) and `get_tools(*, server_name=None)`. Connection TypedDicts:
  `StreamableHttpConnection` (`transport, url, headers, timeout, …`), `SSEConnection` (`timeout`
  is a float), `StdioConnection`, `WebsocketConnection`.
- **T5 — A2A 1.x protobuf types.** Field lists of `AgentCard`, `AgentInterface`,
  `AgentCapabilities`, `AgentSkill`, `Message`, `Part`, `Task`, `TaskStatus`,
  `SendMessageRequest`, `TaskStatusUpdateEvent` and `TaskArtifactUpdateEvent` match the spec
  exactly. `StreamResponse` has a single oneof, `payload`, over
  `task | message | status_update | artifact_update`. `TaskState` also has
  `TASK_STATE_UNSPECIFIED` (treat as unknown). `TransportProtocol` values are `JSONRPC`,
  `HTTP+JSON`, `GRPC`. All listed `a2a.helpers` functions exist.
- **T6 — server wiring.** Import paths and signatures as in the spec.
  `TaskUpdater.add_artifact` additionally accepts `extensions=`. `DefaultRequestHandler` also
  accepts `push_config_store`/`push_sender` (future seam) and `validate_input_modes`.
- **T7 — confirmed bug.** `create_rest_routes(handler, path_prefix="/a2a/x")` still returns a
  `Mount('/{tenant}', …)` at the app root. MAW must filter `Mount` routes out.
- **T8 — client.** `A2ACardResolver.get_agent_card(relative_card_path=None, http_kwargs=None,
  signature_verifier=None)`; `ClientConfig` fields include `streaming`, `polling`,
  `httpx_client`, `supported_protocol_bindings`. Default card path
  `/.well-known/agent-card.json`.
- **T9 — LangGraph.** `add_node` parameters match the spec. Behaviour checked with a scratch
  graph: with branches of unequal length, `defer=True` ran the join once after both branches;
  without it the join ran twice. Parallel writes to a key without a reducer raise
  `InvalidUpdateError`. `GraphInterrupt` is a subclass of `GraphBubbleUp`.
- **T10 — `init_chat_model`** signature matches.
- **T11 — `AsyncPostgresSaver(conn, pipe=None, serde=None)`.** Pool kwargs and the
  one-statement-per-execute rule are exercised in Phase 6.
