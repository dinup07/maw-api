"""Live integrations against the mock MCP server and mock A2A agent (real subprocesses)."""

from __future__ import annotations

import json

import pytest
from conftest import WORKFLOWS_DIR, chain, free_port, wf


async def run(service, raw, input="", **kwargs):
    definition, _ = service.resolve(workflow=raw)
    return await service.start(definition, input, **kwargs)


async def run_catalog(service, workflow_id, input, **kwargs):
    definition, _ = service.resolve(workflow_id=workflow_id)
    return await service.start(definition, input, **kwargs)


def scripted(**params):
    return {"provider": "scripted", "model": "test", "params": params}


def nodes_of(record):
    return [t["node"] for t in record.trace]


# MCP -------------------------------------------------------------------------------------------------
async def test_mcp_node_returns_structured_content(live_service):
    raw = wf(
        [
            {
                "id": "search",
                "type": "mcp",
                "server": "kb",
                "tool": "search_kb",
                "arguments": {"query": "input", "limit": 1},
            }
        ],  # fmt: skip
        chain("search"),
    )
    record = await run(live_service, raw, "vpn password")
    assert record.status == "completed", record.error
    assert record.output["count"] == 1
    assert record.output["articles"][0]["id"] == "KB-101"
    assert record.trace[0]["server"] == "kb" and record.trace[0]["tool"] == "search_kb"


async def test_mcp_tool_error_fails_node(live_service):
    raw = wf(
        [
            {
                "id": "ticket",
                "type": "mcp",
                "server": "kb",
                "tool": "create_ticket",
                "arguments": {"title": "input", "priority": "'urgent'"},
            }
        ],  # fmt: skip
        chain("ticket"),
    )
    record = await run(live_service, raw, "printer on fire")
    assert record.status == "failed"
    assert record.error["node"] == "ticket"
    assert record.error["type"] == "McpToolError"
    assert "priority must be" in record.error["message"]


async def test_tool_discovery_filters_and_cache(live_service):
    ctx = live_service.ctx
    kb = ctx.connections.mcp("kb")
    tools = await ctx.mcp.tools_for([(kb, ["*_ticket"], ["get_*"])])
    assert [t.name for t in tools] == ["create_ticket"]
    assert {t.name for t in await ctx.mcp.list_tools(kb)} == {"search_kb", "create_ticket", "get_ticket"}
    assert await ctx.mcp.list_tools(kb) is await ctx.mcp.list_tools(kb)


async def test_colliding_tool_names_are_prefixed(live_service, mcp_url):
    raw = wf(
        [{"id": "a", "type": "transform", "mapping": {}}], chain("a"), mcp_servers={"kb2": {"url": mcp_url}}
    )
    definition, _ = live_service.resolve(workflow=raw)
    ctx = live_service.ctx
    tools = await ctx.mcp.tools_for(
        [
            (ctx.connections.mcp("kb"), ["search_kb", "get_ticket"], []),
            (ctx.connections.mcp("kb2", definition), ["search_kb"], []),
        ]
    )
    assert sorted(t.name for t in tools) == ["get_ticket", "kb2__search_kb", "kb__search_kb"]


async def test_llm_uses_mcp_tool_in_a_loop(live_service):
    raw = wf(
        [
            {
                "id": "agent",
                "type": "llm",
                "model": "m",
                "tools": {"mcp": [{"server": "kb", "allow": ["create_ticket"]}]},
            }
        ],
        chain("agent"),
        models={
            "m": scripted(
                responses=[
                    {
                        "tool_calls": [
                            {"name": "create_ticket", "args": {"title": "Broken chair", "priority": "low"}}
                        ]
                    },
                    "I opened a ticket for you.",
                ]
            )
        },  # fmt: skip
    )
    record = await run(live_service, raw, "my chair is broken")
    assert record.status == "completed", record.error
    assert record.trace[0]["tool_calls"] == 1
    assert record.text == "I opened a ticket for you."


async def test_inline_mcp_server(service, mcp_url):
    raw = wf(
        [
            {
                "id": "lookup",
                "type": "mcp",
                "server": "mine",
                "tool": "get_ticket",
                "arguments": {"ticket_id": "'TCK-1'"},
            }
        ],
        chain("lookup"),
        mcp_servers={"mine": {"url": mcp_url}},
    )
    record = await run(service, raw)
    assert record.status == "completed", record.error
    assert record.output == {"ticket_id": "TCK-1", "status": "in_progress", "assignee": "service-desk"}


# A2A -------------------------------------------------------------------------------------------------
def a2a_workflow(**node):
    return wf([{"id": "b", "type": "a2a", "agent": "billing", **node}], chain("b"))


async def test_a2a_node_collects_text_and_data(live_service):
    record = await run(live_service, a2a_workflow(), "Was INV-1001 charged twice?")
    assert record.status == "completed", record.error
    out = record.output
    assert "refund of $129.00" in out["text"]
    assert out["data"]["action"] == "refund_issued"
    assert out["state"] == "completed"
    assert out["task_id"]
    assert record.trace[0]["agent"] == "billing" and record.trace[0]["remote_state"] == "completed"


async def test_a2a_streaming_matches_unary(live_service):
    unary = await run(live_service, a2a_workflow(streaming=False), "Is INV-1002 ok?")
    streamed = await run(live_service, a2a_workflow(streaming=True), "Is INV-1002 ok?")
    assert unary.status == streamed.status == "completed"
    assert unary.output["text"] == streamed.output["text"]
    assert unary.output["data"] == streamed.output["data"]
    assert "No duplicate charge" in unary.output["text"]


async def test_a2a_context_kept_across_turns(live_service):
    raw = a2a_workflow()
    first = await run(live_service, raw, "INV-1001?")
    second = await run(live_service, raw, "and INV-1002?", thread_id=first.thread_id)
    assert second.output["context_id"] == first.output["context_id"]
    assert second.output["task_id"] != first.output["task_id"]
    other_thread = await run(live_service, raw, "INV-1002?")
    assert other_thread.output["context_id"] != first.output["context_id"]


async def test_a2a_keep_context_false(live_service):
    raw = a2a_workflow(keep_context=False)
    first = await run(live_service, raw, "INV-1001?")
    second = await run(live_service, raw, "INV-1002?", thread_id=first.thread_id)
    assert second.output["context_id"] != first.output["context_id"]


async def test_llm_calls_agent_as_tool(live_service):
    raw = wf(
        [{"id": "agent", "type": "llm", "model": "m", "tools": {"agents": ["billing"]}}],
        chain("agent"),
        models={
            "m": scripted(
                responses=[
                    {"tool_calls": [{"name": "billing", "args": {"message": "Was INV-1001 charged twice?"}}]},
                    "The billing agent confirmed a refund.",
                ]
            )
        },  # fmt: skip
    )
    record = await run(live_service, raw, "check my invoice")
    assert record.status == "completed", record.error
    assert record.trace[0]["tool_calls"] == 1


async def test_unreachable_agent_fails_run(service):
    raw = wf(
        [
            {"id": "a", "type": "transform", "mapping": {"x": "'1'"}},
            {"id": "b", "type": "a2a", "agent": "ghost"},
        ],
        chain("a", "b"),
        agents={"ghost": {"url": f"http://127.0.0.1:{free_port()}"}},
    )
    record = await run(service, raw, "hello?")
    assert record.status == "failed"
    assert record.error["node"] == "b"
    assert "unreachable" in record.error["message"]


# Catalog workflows end to end ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "route", "expected"),
    [
        ("Was INV-1001 charged twice?", ["classify", "billing", "respond"], "refund"),
        ("my vpn password expired", ["classify", "it_support", "respond"], "self-service portal"),
        ("how much annual leave do I get", ["classify", "hr_lookup", "hr_answer", "respond"], "24 days"),
        ("the coffee machine is broken", ["classify", "open_ticket", "general_reply", "respond"], "TCK-"),
    ],
)
async def test_support_triage_routes(live_service, question, route, expected):
    record = await run_catalog(live_service, "support_triage", question)
    assert record.status == "completed", record.error
    assert nodes_of(record) == route
    assert expected in record.text


async def test_support_triage_billing_details(live_service):
    record = await run_catalog(live_service, "support_triage", "Was INV-1001 charged twice?")
    assert record.output["category"] == "billing"
    assert record.output["billing_details"] == {
        "invoice": "INV-1001", "found": True, "action": "refund_issued", "amount": 129.0,
    }  # fmt: skip


async def test_kb_research_parallel_join(live_service):
    record = await run_catalog(live_service, "kb_research", "vpn password reset")
    assert record.status == "completed", record.error
    assert record.output["top_article"] == "Reset your VPN password"
    assert record.output["question_words"] == 3
    assert record.output["articles_found"] >= 1
    assert nodes_of(record).count("summarize") == 1
    assert nodes_of(record)[-2:] == ["summarize", "result"]


@pytest.mark.parametrize(
    ("question", "fragment"),
    [("Was my invoice charged twice?", "billing agent"), ("my laptop won't turn on", "knowledge base")],
)
async def test_concierge_uses_the_right_tool(live_service, question, fragment):
    record = await run_catalog(live_service, "concierge", question)
    assert record.status == "completed", record.error
    assert record.trace[0]["tool_calls"] == 1
    assert fragment in record.text


async def test_catalog_files_validate_against_live_connections(live_service):
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        definition, _ = live_service.resolve(workflow=json.loads(path.read_text()))
        assert definition.id == path.stem


async def test_connections_file_env_references_are_resolved(tmp_path, monkeypatch, mcp_url, billing_url):
    """Regression: ${env:...} in the trusted file must be substituted for both MCP and A2A URLs."""
    from contextlib import AsyncExitStack

    from conftest import build_service, make_settings

    monkeypatch.setenv("TEST_KB_URL", mcp_url)
    monkeypatch.delenv("TEST_UNSET_BILLING_URL", raising=False)
    path = tmp_path / "env-connections.yaml"
    path.write_text(
        "mcp_servers:\n  kb:\n    url: ${env:TEST_KB_URL}\n"
        f"agents:\n  billing:\n    url: ${{env:TEST_UNSET_BILLING_URL:-{billing_url}}}\n"
    )
    async with AsyncExitStack() as stack:
        svc = await build_service(make_settings(tmp_path, connections_file=path), stack)
        record = await run_catalog(svc, "support_triage", "Was INV-1001 charged twice?")
        assert record.status == "completed", record.error
        assert "refund" in record.text
        record = await run_catalog(svc, "kb_research", "vpn password reset")
        assert record.output["top_article"] == "Reset your VPN password"
