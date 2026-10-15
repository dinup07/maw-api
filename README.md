# MAW — Multi-Agent Workflow runtime

MAW executes multi-agent workflows defined as JSON. Each definition is validated, compiled into a
LangGraph `StateGraph` and run with LLM, MCP, A2A, local-function, transform and human nodes.

> Status: under construction (Phase 1 — scaffold and workflow contract).

## Quick start

```bash
uv sync
uv run maw validate workflows/*.json
uv run maw schema
uv run pytest -q
```
