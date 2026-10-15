# MAW curl walkthrough

Start the stack first, either with `docker compose up --build -d` or locally:

```bash
uv run python mocks/mcp_kb_server.py --host 127.0.0.1 --port 8001 &
uv run python mocks/a2a_billing_agent.py --host 127.0.0.1 --port 9001 &
uv run maw serve
```

```bash
B=http://localhost:8000
J='content-type: application/json'
```

If `MAW_API_KEYS` is set, add `-H 'X-API-Key: <key>'` to every call below except the health
probes and agent cards.

## Health

```bash
curl -s $B/health
curl -s $B/ready            # "durable": true when MAW_DATABASE_URL is set
curl -s $B/metrics | grep maw_runs_total
```

## Run catalog workflows

```bash
# Conditional routing: billing -> A2A agent, IT -> LLM + MCP tools, HR -> MCP lookup, else a ticket
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "support_triage", "input": "Was INV-1001 charged twice?"}'
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "support_triage", "input": "my vpn password expired"}'
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "support_triage", "input": "how much annual leave do I get"}'
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "support_triage", "input": "the coffee machine is broken"}'

# Parallel fan-out and a deferred join
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "kb_research", "input": "vpn password reset"}'

# One LLM using MCP tools and an A2A agent as tools
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "concierge", "input": "Was my invoice charged twice?"}'
```

## Conversations

```bash
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "hello", "input": "one", "thread_id": "demo-thread"}'
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "hello", "input": "two", "thread_id": "demo-thread"}'
```

## Streaming (Server-Sent Events)

```bash
curl -sN $B/v1/runs/stream -H "$J" -d '{"workflow_id": "kb_research", "input": "vpn password reset"}'
```

## Async runs

```bash
RUN=$(curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "kb_research", "input": "leave policy", "mode": "async"}' \
      | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
curl -s $B/v1/runs/$RUN
curl -s "$B/v1/runs?workflow_id=kb_research&limit=5"
```

## Human in the loop

```bash
RUN=$(curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "approval", "input": "INV-1001 was charged twice"}' \
      | python3 -c 'import sys, json; r = json.load(sys.stdin); print(r["id"]); print(r["interrupt"], file=sys.stderr)')
curl -s $B/v1/runs/$RUN/resume -H "$J" -d '{"value": "please add a goodwill credit"}'   # interrupted again, revised draft
curl -s $B/v1/runs/$RUN/resume -H "$J" -d '{"value": "approve"}'                         # completed, review_rounds: 2
curl -s $B/v1/runs/$RUN/resume -H "$J" -d '{"value": "again"}'                           # 409
```

Streaming resume: `curl -sN $B/v1/runs/$RUN/resume/stream -H "$J" -d '{"value": "approve"}'`.
Cancel a run: `curl -s -X POST $B/v1/runs/$RUN/cancel`.

## Inline workflows

```bash
curl -s $B/v1/runs -H "$J" -d '{
  "workflow": {
    "nodes": [{"id": "calc", "type": "function", "function": "calculator", "arguments": {"expression": "input"}}],
    "edges": [{"source": "start", "target": "calc"}, {"source": "calc", "target": "end"}]
  },
  "input": "2 ** 10 + 1"
}'

# A reference-format workflow, sent in the reference request shape
curl -s $B/v1/runs -H "$J" -d @examples/reference_format.json

# The support triage workflow with real OpenAI models (needs OPENAI_API_KEY on the server)
curl -s $B/v1/runs -H "$J" -d "{\"workflow\": $(cat examples/openai_support_triage.json), \"input\": \"my vpn password expired\"}"
```

## Validate, register and inspect workflows

```bash
curl -s $B/v1/schema | head -c 300
curl -s $B/v1/workflows/validate -H "$J" -d @workflows/kb_research.json   # includes a Mermaid diagram
curl -s $B/v1/workflows
curl -s $B/v1/workflows/support_triage

curl -s $B/v1/workflows -H "$J" -d '{"workflow": {
  "id": "shouty", "expose": {"a2a": true},
  "nodes": [{"id": "t", "type": "transform", "mapping": {"text": "You said: {{ input }}"}}],
  "edges": [{"source": "start", "target": "t"}, {"source": "t", "target": "end"}]}}'
curl -s $B/v1/runs -H "$J" -d '{"workflow_id": "shouty", "input": "hi"}'
curl -s $B/a2a/shouty/.well-known/agent-card.json
curl -s -X DELETE $B/v1/workflows/shouty -o /dev/null -w '%{http_code}\n'   # 204
```

## Connections

```bash
curl -s $B/v1/connections
curl -s $B/v1/connections/mcp/kb/tools
curl -s $B/v1/connections/agents/billing/card
```

## A2A

```bash
curl -s $B/a2a/support_triage/.well-known/agent-card.json

# JSON-RPC SendMessage
curl -s $B/a2a/support_triage -H "$J" -H 'A2A-Version: 1.0' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "how much annual leave do I get"}]}}
}'

# MAW calling its own A2A agent from an inline workflow
curl -s $B/v1/runs -H "$J" -d '{
  "workflow": {
    "agents": {"self": {"url": "http://localhost:8000/a2a/support_triage"}},
    "nodes": [{"id": "ask", "type": "a2a", "agent": "self", "streaming": true},
              {"id": "out", "type": "transform", "mapping": {"text": "Via A2A: {{ nodes.ask.text }}", "category": "nodes.ask.data.category"}}],
    "edges": [{"source": "start", "target": "ask"}, {"source": "ask", "target": "out"}, {"source": "out", "target": "end"}]
  },
  "input": "my vpn password expired"
}'
```
