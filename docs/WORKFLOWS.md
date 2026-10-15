# Writing MAW workflows

A workflow is a JSON document with **models**, **connections**, **nodes** and **edges**. Every
object rejects unknown fields, so a typo like `sytem_prompt` fails validation instead of being
ignored. The full JSON Schema is served at `GET /v1/schema` and printed by `maw schema`.

Validate before running:

```bash
uv run maw validate my_workflow.json
curl -s localhost:8000/v1/workflows/validate -H 'content-type: application/json' -d @my_workflow.json
```

## Top level

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | `"inline"` | `^[A-Za-z_][A-Za-z0-9_-]{0,63}$`. Catalog key and A2A path segment. Files in `workflows/` default to their file name |
| `version` | string | — | Informational; caching uses a hash of the content |
| `description` | string | — | |
| `models` | object | `{}` | Named models (below) |
| `mcp_servers` | object | `{}` | Inline MCP servers (untrusted) |
| `agents` | object | `{}` | Inline A2A agents (untrusted) |
| `nodes` | array | required | |
| `edges` | array | required | |
| `limits` | object | `{}` | `recursion_limit` (LangGraph supersteps), `max_node_visits` (loop guard default) |
| `expose` | object | `{}` | `a2a`, `name`, `description`, `skills`, `streaming` — see *Exposing as an A2A agent* |

## Models

```json
"models": {
  "router": { "provider": "openai", "model": "gpt-4o-mini", "temperature": 0 },
  "local": { "provider": "openai", "model": "qwen3", "base_url": "http://llm.internal/v1",
             "api_key": "${secret:LOCAL_LLM_KEY}" }
}
```

| Field | Notes |
|---|---|
| `provider` | Any LangChain `init_chat_model` provider (`openai`, `anthropic`, `google_genai`, `ollama`, `azure_openai`, `bedrock_converse`, `groq`, `mistralai`, …) or `scripted` |
| `model` | Model name |
| `base_url` | Checked against `MAW_ALLOWED_OUTBOUND_HOSTS` |
| `api_key` | Use `${secret:NAME}` (reads `MAW_SECRET_NAME`). Literal keys need `MAW_ALLOW_INLINE_API_KEYS=true` |
| `temperature`, `max_tokens` | |
| `params` | Extra keyword arguments for the chat model |

A node's `model` resolves as: a key in `models`; else `"scripted"` (the echo model); else
`"provider:model"` (e.g. `"anthropic:claude-…"`); else a bare model name whose provider LangChain
infers. Provider packages for OpenAI and Anthropic are installed; others need their LangChain
package.

### The scripted model

`provider: "scripted"` is deterministic and needs no keys, for tests and demos. In `params`:

- `rules`: `[{"contains": "word" | ["words"], "respond": response | [responses]}]` — the first rule
  whose needle occurs (case-insensitively) in the latest human message wins;
- `responses`: the fallback response or list of responses.

A list of responses is stepped through by the number of AI messages since the human message, which
walks a tool loop. A response is a string, `{"content": "..."}`, `{"tool_calls": [{"name", "args"}]}`,
or (for structured output) a JSON object. With no rules or responses it answers `echo: <input>`.

## Connections

Workflows reference MCP servers and A2A agents by name. Names resolve first to the workflow's own
`mcp_servers`/`agents` (untrusted), then to the server-side `config/connections.yaml` (trusted).

```json
"mcp_servers": {
  "kb": { "url": "https://kb.example.com/mcp", "headers": { "Authorization": "Bearer ${secret:KB_TOKEN}" } }
},
"agents": {
  "billing": { "url": "https://billing.example.com", "description": "Answers invoice questions." }
}
```

MCP server: `transport` (`streamable_http` default, `sse`, `stdio`), `url`, `headers`, `command`,
`args`, `env`, `timeout_s`. **`stdio` servers run local commands and are only allowed in the
server-side file.** A2A agent: `url` (the card is fetched from `<url>/.well-known/agent-card.json`),
`card_path`, `headers`, `timeout_s`, `description`.

Secrets: `${secret:NAME}` reads `MAW_SECRET_NAME` anywhere. `${env:NAME}` and
`${env:NAME:-default}` only work in the trusted connections file.

## Nodes

Common fields on every node:

| Field | Default | Meaning |
|---|---|---|
| `id` | required | Same pattern as the workflow id; not `start` or `end` |
| `description` | — | |
| `write_output` | `true` | Also replace the run's `output` with this node's result |
| `retry` | `{"max_attempts": 1, "backoff_s": 0.5}` | Up to 10 attempts; delay `backoff_s * 2^(attempt-1)` |
| `timeout_s` | — | Per attempt |
| `on_error` | `"fail"` | `"continue"` records `{"error", "error_type"}` as the result and moves on |
| `defer` | `false` | Join: run once after all in-flight parallel branches finish |
| `max_visits` | — | Loop guard for this node (default from `limits.max_node_visits`, then 10) |

Results are stored under `nodes.<id>` (a non-object result becomes `{"result": value}`).

### `llm`

```json
{ "id": "classify", "type": "llm", "model": "router",
  "system_prompt": "Classify the request.",
  "user_message": "Customer {{ input.customer.name }} says: {{ input.text }}",
  "output_schema": { "type": "object", "properties": { "category": { "type": "string" } }, "required": ["category"] },
  "tools": { "mcp": [{ "server": "kb", "allow": ["search_*"] }], "agents": ["billing"], "functions": ["calculator"] },
  "max_tool_calls": 4, "history": "all" }
```

| Field | Default | Meaning |
|---|---|---|
| `model` | required | See *Models* |
| `system_prompt`, `user_message` | — | Templates. Without `user_message`, the run input is the user message |
| `inputs` | `{}` | Named values for templates (`{"articles": "nodes.search.articles"}` → `{{ articles }}`) |
| `tools.mcp` | `[]` | Server names, or `{"server", "allow", "block"}` with exact names or globs; empty `allow` = all, `block` wins. Tools whose names collide across servers become `<server>__<tool>` |
| `tools.agents` | `[]` | A2A agents the model can call as tools (with a `message` argument) |
| `tools.functions` | `[]` | Registered local functions |
| `max_tool_calls` | 8 | Budget; when exceeded the model is asked to answer with what it has |
| `output_schema` | — | JSON Schema of an object → structured output, validated |
| `recover_text_field` | — | On a schema mismatch, return `{field: raw_output}` instead of failing |
| `history` | `"none"` | `"all"` or a number: how much of the thread's conversation the model sees |
| `append_to_messages` | `true` | Add the final answer to the conversation |

Result: `{"text": ...}`, or the structured object. Tool errors are returned to the model as error
messages so it can recover; they don't fail the node.

### `mcp`

Calls one MCP tool deterministically. Arguments are value expressions; `null` results are dropped.

```json
{ "id": "lookup", "type": "mcp", "server": "kb", "tool": "search_kb", "arguments": { "query": "input", "limit": 1 } }
```

The result is the tool's structured content (or parsed JSON text). An MCP tool error fails the node,
so `retry` and `on_error` apply.

### `a2a`

Sends a message to an A2A agent.

```json
{ "id": "billing", "type": "a2a", "agent": "billing", "message": "{{ input }}", "data": { "customer": "input.customer_id" } }
```

| Field | Default | Meaning |
|---|---|---|
| `message` | run input | Template |
| `data` | — | Sent as a data part (value expressions) |
| `inputs` | `{}` | Named template values |
| `streaming` | `false` | Use the streaming protocol |
| `keep_context` | `true` | Reuse the remote conversation on later runs of the same thread |
| `append_to_messages` | `true` | |

Result: `{"text", "data", "state", "task_id", "context_id"}`. A remote `input_required` state is
returned (route on `nodes.<id>.state`); `failed`, `rejected`, `canceled` and `auth_required` fail
the node.

### `function`

```json
{ "id": "stats", "type": "function", "function": "text.stats", "arguments": { "text": "input" } }
```

Built-ins: `calculator` (`expression`; safe arithmetic only), `json.parse` (`text`), `text.join`
(`items`, `separator`), `text.stats` (`text`), `dict.merge` (`objects`). Register your own with
`@register_function("name")` in a module listed in `MAW_FUNCTION_MODULES`.

### `transform`

Builds a result from state without any external call. `null` values are dropped.

```json
{ "id": "respond", "type": "transform",
  "mapping": { "text": "nodes.billing.text || nodes.general.text", "category": "nodes.classify.category", "source": "'kb'" } }
```

### `human`

Pauses the run (status `interrupted`) with a prompt. Resume with `POST /v1/runs/{id}/resume`
`{"value": ...}`; the result is the value if it's an object, else `{"response": value}`.

```json
{ "id": "review", "type": "human", "prompt": "Approve this draft?\n\n{{ nodes.draft.text }}" }
```

## Values, paths and templates

One resolver is used for `inputs`, `arguments`, `mapping`, `data`, condition fields and templates.

| Expression | Result |
|---|---|
| `"input.customer.id"` | A path. **Bare strings are paths, not literals** |
| `"classify.category"` | Paths not starting with a state key are looked up under `nodes` |
| `"messages.-1.content"` | Negative list indexes work |
| `"nodes.x.items.length"` | `length`/`len` of a string |
| `"'literal'"` | A quoted string is a literal |
| `"a.b \|\| c.d \|\| 'default'"` | First non-null option |
| `"{{ nodes.search.articles }}"` | A single placeholder keeps the value's type (here a list) |
| `"Hi {{ input.name }}"` | Several placeholders or surrounding text render a string |
| `3`, `true`, `null`, objects, arrays | Literals (resolved recursively) |

State keys: `input`, `messages`, `nodes`, `output`, `vars`, `visits`, `errors`, `trace`. Anything
missing resolves to `null`; resolution never fails.

## Edges

```json
{ "source": "start", "target": "classify" }
{ "source": "start", "target": ["search", "stats"] }
{ "source": "classify", "target": "fallback",
  "condition": [
    { "field": "output.category", "operator": "equals", "value": "billing", "target": "billing" },
    { "field": "nodes.classify.score", "operator": "gt", "value": 0.8, "target": ["a", "b"] }
  ] }
```

- `start` and `end` are the entry and exit. Every reachable node needs an outgoing edge, and `end`
  must be reachable.
- Conditions are checked in order and **the first match wins**; otherwise `target` is used, or
  `end` if there is none. A source with a conditional edge must have exactly one edge.
- A list target fans out in parallel; several unconditional edges from one source also fan out.
- Operators: `equals`, `not_equals`, `in`, `not_in` (string comparisons ignore case and surrounding
  spaces), `contains` (substring, list item, or object key), `gt`, `gte`, `lt`, `lte` (numeric;
  false if either side isn't a number), `exists`, `not_exists`, `matches` (regex search).

### Joins and loops

A node with `"defer": true` waits for every in-flight branch and runs once; without it, a node that
several branches lead to runs once per branch. Loops are allowed (an edge back to an earlier node)
and are bounded by `max_visits` per node and `limits.recursion_limit` overall; exceeding either
fails the run.

## Conversations

Pass the same `thread_id` to several runs to continue a conversation. `messages` and `vars` carry
over; `nodes`, `output`, `trace`, `errors` and `visits` start fresh each run. Use `history` on `llm`
nodes to show the model earlier turns.

## Exposing as an A2A agent

```json
"expose": { "a2a": true, "name": "Support triage", "description": "Routes support requests.",
            "skills": [{ "id": "triage", "name": "Triage", "description": "Route a request", "tags": ["support"],
                         "examples": ["Was INV-1001 charged twice?"] }],
            "streaming": true }
```

The agent card is served at `/a2a/<id>/.well-known/agent-card.json`, with JSON-RPC at `POST /a2a/<id>`
and the HTTP+JSON binding under `/a2a/<id>/`. The A2A `context_id` becomes the MAW thread id. Text
parts become the input; a single data object is merged into it (`{"text": ..., **data}`). Each
completed node is streamed as a `working` status update (metadata `node`, `maw_run_id`), the output
arrives as an artifact named `result`, and a `human` node becomes `input_required` — the next
message on the same task resumes the run.

## The reference format

Definitions in the reference MAW format (with a `workflow_config` wrapper, `*_node` types,
`version_id`, a `models` list, or a top-level `config`) are translated automatically, with warnings
for anything dropped. Highlights: `llm_node` → `llm`, `enrichment_node` → `transform`,
`function_node` → `mcp`, `tool_node` → `function`, `input_mapping` → `inputs`,
`structured_output_schema` → `output_schema`, `skip_output_channel` → `!write_output`, `{x}` →
`{{ x }}` in `user_message`, `api_key_env_var: X` → `${secret:X}`, and `config.recursion_limit` /
`config.max_iterations` → `limits`. See `examples/reference_format.json`.
