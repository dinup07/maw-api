"""HTTP API and A2A server, against a real uvicorn server running in a background thread."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import get_text_parts, new_text_part
from a2a.types import Message, Role, SendMessageRequest, TaskState
from conftest import ServerThread, free_port, make_settings

# --------------------------------------------------------------------------------------------------
# Server fixtures
# --------------------------------------------------------------------------------------------------


def _write_connections(directory: Path, mcp_url: str, billing_url: str) -> Path:
    path = directory / "connections.yaml"
    path.write_text(f"mcp_servers:\n  kb:\n    url: {mcp_url}\nagents:\n  billing:\n    url: {billing_url}\n")
    return path


def _settings(tmp: Path, mcp_url: str, billing_url: str, **overrides):
    port = free_port()
    return make_settings(
        tmp,
        connections_file=_write_connections(tmp, mcp_url, billing_url),
        port=port,
        public_url=f"http://127.0.0.1:{port}",
        **overrides,
    )


@pytest.fixture(scope="module")
def api(tmp_path_factory, mcp_url, billing_url):
    with ServerThread(_settings(tmp_path_factory.mktemp("api"), mcp_url, billing_url)) as server:
        yield server.url


@pytest.fixture
async def client(api):
    async with httpx.AsyncClient(base_url=api, timeout=30) as http:
        yield http


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------


def chain_edges(*ids):
    path = ["start", *ids, "end"]
    return [{"source": a, "target": b} for a, b in zip(path, path[1:], strict=False)]


async def a2a_send(base_url, text, *, streaming=False, task_id=None, context_id=None):
    async with httpx.AsyncClient(timeout=30) as http:
        card = await A2ACardResolver(http, base_url).get_agent_card()
        a2a = ClientFactory(ClientConfig(streaming=streaming, httpx_client=http)).create(card)
        message = Message(message_id=uuid.uuid4().hex, role=Role.ROLE_USER, parts=[new_text_part(text)])
        if task_id:
            message.task_id = task_id
        if context_id:
            message.context_id = context_id
        return [r async for r in a2a.send_message(SendMessageRequest(message=message))]


def final_task_state(responses):
    states = []
    for resp in responses:
        kind = resp.WhichOneof("payload")
        if kind == "task":
            states.append(resp.task.status.state)
        elif kind == "status_update":
            states.append(resp.status_update.status.state)
    return states[-1]


def artifact_texts(responses):
    texts = []
    for resp in responses:
        kind = resp.WhichOneof("payload")
        if kind == "task":
            texts += [t for a in resp.task.artifacts for t in get_text_parts(a.parts)]
        elif kind == "artifact_update":
            texts += get_text_parts(resp.artifact_update.artifact.parts)
    return "\n".join(texts)


def ids_of(responses):
    for resp in responses:
        kind = resp.WhichOneof("payload")
        if kind == "task":
            return resp.task.id, resp.task.context_id
        if kind == "status_update":
            return resp.status_update.task_id, resp.status_update.context_id
    raise AssertionError("no task in response")


def parse_sse(body: str):
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


# 1 ---------------------------------------------------------------------------------------------------
async def test_health_ready_metrics(client):
    assert (await client.get("/health")).json() == {"status": "ok", "version": "0.1.0"}
    ready = (await client.get("/ready")).json()
    assert ready["status"] == "ready" and ready["workflows"] == 5 and ready["durable"] is False
    assert set(ready["graph_cache"]) == {"size", "capacity"}
    await client.post("/v1/runs", json={"workflow_id": "hello", "input": "hi"})
    metrics = (await client.get("/metrics")).text
    assert 'maw_runs_total{outcome="completed",workflow_id="hello"}' in metrics


# 2 ---------------------------------------------------------------------------------------------------
async def test_catalog_inline_and_reference_runs(client):
    catalog = (await client.post("/v1/runs", json={"workflow_id": "hello", "input": "hey"})).json()
    assert catalog["status"] == "completed" and catalog["text"] == "echo: hey"

    inline = {
        "nodes": [{"id": "t", "type": "transform", "mapping": {"text": "Got {{ input.n }}"}}],
        "edges": chain_edges("t"),
    }
    resp = await client.post("/v1/runs", json={"workflow": inline, "input": {"n": 42}})
    assert resp.status_code == 200 and resp.json()["text"] == "Got 42"

    reference = {
        "workflow_config": {
            "nodes": [{"id": "echo", "type": "enrichment_node", "mapping": {"text": "input"}}],
            "edges": chain_edges("echo"),
        },
        "input_prompt": "legacy",
        "session_id": "sess-1",
        "async": "false",
        "user_id": "someone",
    }
    body = (await client.post("/v1/runs", json=reference)).json()
    assert body["text"] == "legacy" and body["thread_id"] == "sess-1"


# 3 ---------------------------------------------------------------------------------------------------
async def test_error_mapping(client):
    bad = {"nodes": [{"id": "x", "type": "bogus"}], "edges": chain_edges("x")}
    resp = await client.post("/v1/runs", json={"workflow": bad})
    assert resp.status_code == 422 and resp.json()["detail"] == "invalid workflow" and resp.json()["errors"]
    resp = await client.post("/v1/runs", json={"input": "nothing to run"})
    assert resp.status_code == 422 and "exactly one" in resp.json()["errors"][0]
    resp = await client.get("/v1/runs/run_nope")
    assert resp.status_code == 404 and resp.json() == {"detail": "run 'run_nope' not found"}


async def test_run_failure_is_not_an_http_error(client):
    failing = {"nodes": [{"id": "f", "type": "function", "function": "test.boom"}], "edges": chain_edges("f")}
    resp = await client.post("/v1/runs", json={"workflow": failing})
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed" and resp.json()["error"]["node"] == "f"


# 4 ---------------------------------------------------------------------------------------------------
async def test_validate_list_and_schema(client):
    valid = (await client.post("/v1/workflows/validate", json={"nodes": [{"id": "a", "type": "llm", "model": "scripted"}],
                                                               "edges": chain_edges("a")})).json()  # fmt: skip
    assert valid["valid"] is True and "graph TD" in valid["mermaid"] and len(valid["hash"]) == 64
    assert valid["normalized"]["nodes"] == [{"id": "a", "type": "llm", "model": "scripted"}]
    invalid = (await client.post("/v1/workflows/validate", json={"nodes": [], "edges": []})).json()
    assert invalid["valid"] is False and invalid["errors"]

    listing = (await client.get("/v1/workflows")).json()
    triage = next(w for w in listing if w["id"] == "support_triage")
    assert (
        triage["source"] == "file"
        and triage["node_count"] == 8
        and triage["a2a_url"].endswith("/a2a/support_triage")
    )
    detail = (await client.get("/v1/workflows/approval")).json()
    assert detail["summary"]["a2a_url"] is None and "graph TD" in detail["mermaid"]
    assert (await client.get("/v1/workflows/nope")).status_code == 404
    assert (await client.get("/v1/schema")).json()["title"] == "WorkflowDefinition"


async def test_register_and_delete_rules(client):
    raw = {"id": "tmp_api_wf", "nodes": [{"id": "t", "type": "transform", "mapping": {"text": "input"}}],
           "edges": chain_edges("t")}  # fmt: skip
    created = await client.post("/v1/workflows", json={"workflow": raw})
    assert created.status_code == 201 and created.json()["source"] == "api"
    assert (await client.post("/v1/runs", json={"workflow_id": "tmp_api_wf", "input": "x"})).json()[
        "text"
    ] == "x"
    assert (await client.post("/v1/workflows", json={"workflow": {**raw, "id": "hello"}})).status_code == 422
    assert (await client.delete("/v1/workflows/hello")).status_code == 422
    assert (await client.delete("/v1/workflows/tmp_api_wf")).status_code == 204
    assert (await client.delete("/v1/workflows/tmp_api_wf")).status_code == 404


# 5 ---------------------------------------------------------------------------------------------------
async def test_connections(client):
    described = (await client.get("/v1/connections")).json()
    assert set(described["mcp_servers"]) == {"kb"} and set(described["agents"]) == {"billing"}
    assert "headers" not in described["mcp_servers"]["kb"]
    tools = (await client.get("/v1/connections/mcp/kb/tools")).json()
    assert {t["name"] for t in tools} == {"search_kb", "create_ticket", "get_ticket"}
    assert all(t["input_schema"] for t in tools)
    card = (await client.get("/v1/connections/agents/billing/card")).json()
    assert card["name"] == "billing-agent"
    assert (await client.get("/v1/connections/mcp/nope/tools")).status_code == 404


# 6 ---------------------------------------------------------------------------------------------------
async def test_sse_stream(client):
    async with client.stream(
        "POST", "/v1/runs/stream", json={"workflow_id": "kb_research", "input": "vpn password reset"}
    ) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        body = "".join([chunk async for chunk in resp.aiter_text()])
    events = parse_sse(body)
    names = [name for name, _ in events]
    assert names[0] == "run_created" and names[1] == "run_started" and names[-1] == "run_completed"
    assert {data["data"]["node"] for name, data in events if name == "node_completed"} == {
        "search",
        "stats",
        "summarize",
        "result",
    }
    assert events[-1][1]["data"]["output"]["top_article"] == "Reset your VPN password"


# 7 ---------------------------------------------------------------------------------------------------
async def test_async_approval_flow(client):
    started = await client.post(
        "/v1/runs", json={"workflow_id": "approval", "input": "INV-1001", "mode": "async"}
    )
    assert started.status_code == 202
    run_id = started.json()["id"]
    for _ in range(100):
        body = (await client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] == "interrupted":
            break
        await asyncio.sleep(0.05)
    assert body["status"] == "interrupted" and "Draft:" in body["interrupt"][0]["prompt"]

    async with client.stream(
        "POST", f"/v1/runs/{run_id}/resume/stream", json={"value": "add a credit"}
    ) as resp:
        events = parse_sse("".join([c async for c in resp.aiter_text()]))
    assert events[0][0] == "run_resumed" and events[-1][0] == "interrupted"

    done = (await client.post(f"/v1/runs/{run_id}/resume", json={"value": "approve"})).json()
    assert (
        done["status"] == "completed"
        and done["output"]["approved"] is True
        and done["output"]["review_rounds"] == 2
    )
    assert (await client.post(f"/v1/runs/{run_id}/cancel")).status_code == 409
    assert (await client.post(f"/v1/runs/{run_id}/resume", json={"value": "again"})).status_code == 409

    listed = (await client.get("/v1/runs", params={"workflow_id": "approval", "status": "completed"})).json()
    assert run_id in [r["id"] for r in listed]


# 8 ---------------------------------------------------------------------------------------------------
async def test_catalog_workflow_over_a2a(client, api):
    card = (await client.get("/a2a/support_triage/.well-known/agent-card.json")).json()
    assert card["name"] == "Support triage"
    assert {i["protocolBinding"] for i in card["supportedInterfaces"]} == {"JSONRPC", "HTTP+JSON"}

    streamed = await a2a_send(f"{api}/a2a/support_triage", "Was INV-1001 charged twice?", streaming=True)
    nodes = [
        r.status_update.metadata["node"]
        for r in streamed
        if r.WhichOneof("payload") == "status_update" and "node" in r.status_update.metadata
    ]
    assert nodes == ["classify", "billing", "respond"]
    assert "refund" in artifact_texts(streamed)
    assert final_task_state(streamed) == TaskState.TASK_STATE_COMPLETED

    unary = await a2a_send(f"{api}/a2a/support_triage", "how much annual leave do I get")
    assert final_task_state(unary) == TaskState.TASK_STATE_COMPLETED
    assert "24 days" in artifact_texts(unary)


# 9 ---------------------------------------------------------------------------------------------------
async def test_registered_workflow_exposed_with_human_input(client, api):
    raw = {
        "id": "ship_gate",
        "nodes": [
            {"id": "ask", "type": "human", "prompt": "Approve '{{ input }}'?"},
            {"id": "decide", "type": "transform", "mapping": {"text": "Decision: {{ nodes.ask.response }}"}},
        ],
        "edges": chain_edges("ask", "decide"),
        "expose": {"a2a": True},
    }
    assert (await client.post("/v1/workflows", json={"workflow": raw})).status_code == 201
    try:
        first = await a2a_send(f"{api}/a2a/ship_gate", "ship v2")
        assert final_task_state(first) == TaskState.TASK_STATE_INPUT_REQUIRED
        task = next(r.task for r in first if r.WhichOneof("payload") == "task")
        assert get_text_parts(task.status.message.parts) == ["Approve 'ship v2'?"]

        task_id, context_id = ids_of(first)
        second = await a2a_send(f"{api}/a2a/ship_gate", "approved", task_id=task_id, context_id=context_id)
        assert final_task_state(second) == TaskState.TASK_STATE_COMPLETED
        assert "Decision: approved" in artifact_texts(second)
    finally:
        await client.delete("/v1/workflows/ship_gate")


# 10 --------------------------------------------------------------------------------------------------
async def test_maw_calls_its_own_a2a_agent(client, api):
    raw = {
        "agents": {"self": {"url": f"{api}/a2a/support_triage"}},
        "nodes": [
            {"id": "ask", "type": "a2a", "agent": "self", "streaming": True},
            {
                "id": "out",
                "type": "transform",
                "mapping": {"text": "Via A2A: {{ nodes.ask.text }}", "category": "nodes.ask.data.category"},
            },
        ],  # fmt: skip
        "edges": chain_edges("ask", "out"),
    }
    body = (await client.post("/v1/runs", json={"workflow": raw, "input": "my vpn password expired"})).json()
    assert body["status"] == "completed", body["error"]
    assert body["output"]["category"] == "it"
    assert body["text"].startswith("Via A2A:")


# 11 --------------------------------------------------------------------------------------------------
async def test_every_mounted_agent_card_is_served(client):
    for workflow_id in ("hello", "support_triage", "concierge"):
        resp = await client.get(f"/a2a/{workflow_id}/.well-known/agent-card.json")
        assert resp.status_code == 200, workflow_id
    assert (await client.get("/a2a/approval/.well-known/agent-card.json")).status_code == 404


# 12 --------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def secured_api(tmp_path_factory, mcp_url, billing_url):
    settings = _settings(tmp_path_factory.mktemp("secured"), mcp_url, billing_url, api_keys=["k-1", "k-2"])
    with ServerThread(settings) as server:
        yield server.url


async def test_api_key_auth(secured_api):
    async with httpx.AsyncClient(base_url=secured_api, timeout=30) as http:
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/ready")).status_code == 200
        assert (await http.get("/a2a/hello/.well-known/agent-card.json")).status_code == 200
        denied = await http.get("/v1/workflows")
        assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
        assert denied.json() == {"detail": "missing or invalid API key"}
        assert (
            await http.post("/a2a/hello", json={"jsonrpc": "2.0", "id": 1, "method": "x"})
        ).status_code == 401
        assert (await http.get("/v1/workflows", headers={"Authorization": "Bearer k-1"})).status_code == 200
        assert (await http.get("/v1/workflows", headers={"X-API-Key": "k-2"})).status_code == 200
        assert (await http.get("/v1/workflows", headers={"X-API-Key": "wrong"})).status_code == 401
        assert (await http.get("/v1/workflows", headers={"Authorization": "Basic k-1"})).status_code == 401
