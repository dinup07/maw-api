"""Engine and runtime behaviour with the scripted model (no network)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from conftest import chain, wf

from maw.runtime import RunStateError


async def run(service, raw, input="", **kwargs):
    definition, _ = service.resolve(workflow=raw)
    return await service.start(definition, input, **kwargs)


def scripted(**params):
    return {"provider": "scripted", "model": "test", "params": params}


def trace_nodes(record):
    return [t["node"] for t in record.trace]


T = {"type": "transform"}


# 1 ---------------------------------------------------------------------------------------------------
async def test_conditional_routing_with_structured_output(service):
    raw = wf(
        [
            {
                "id": "classify",
                "type": "llm",
                "model": "router",
                "output_schema": {
                    "type": "object",
                    "properties": {"category": {"type": "string"}},
                    "required": ["category"],
                },
            },
            {"id": "billing", **T, "mapping": {"text": "'to billing'"}},
            {"id": "other", **T, "mapping": {"text": "'to other'"}},
        ],
        [
            {"source": "start", "target": "classify"},
            {
                "source": "classify",
                "target": "other",
                "condition": [{"field": "output.category", "value": "billing", "target": "billing"}],
            },
            {"source": "billing", "target": "end"},
            {"source": "other", "target": "end"},
        ],  # fmt: skip
        models={
            "router": scripted(
                rules=[{"contains": "invoice", "respond": {"category": "billing"}}],
                responses={"category": "other"},
            )
        },  # fmt: skip
    )
    billing = await run(service, raw, "my invoice is wrong")
    assert billing.status == "completed"
    assert trace_nodes(billing) == ["classify", "billing"]
    assert billing.text == "to billing"
    other = await run(service, raw, "hello")
    assert trace_nodes(other) == ["classify", "other"]


# 2-3 -------------------------------------------------------------------------------------------------
def join_workflow(defer: bool):
    return wf(
        [
            {
                "id": "a1",
                "type": "function",
                "function": "test.sleep",
                "arguments": {"seconds": 0.01, "tag": "'a1'"},
            },
            {
                "id": "a2",
                "type": "function",
                "function": "test.sleep",
                "arguments": {"seconds": 0.01, "tag": "'a2'"},
            },
            {
                "id": "b1",
                "type": "function",
                "function": "test.sleep",
                "arguments": {"seconds": 0.01, "tag": "'b1'"},
            },
            {"id": "join", **T, "defer": defer, "mapping": {"tags": ["nodes.a2.tag", "nodes.b1.tag"]}},
        ],
        [
            {"source": "start", "target": ["a1", "b1"]},
            {"source": "a1", "target": "a2"},
            {"source": "a2", "target": "join"},
            {"source": "b1", "target": "join"},
            {"source": "join", "target": "end"},
        ],
    )


async def test_parallel_branches_join_once_with_defer(service):
    record = await run(service, join_workflow(defer=True))
    assert record.status == "completed"
    assert trace_nodes(record).count("join") == 1
    assert trace_nodes(record)[-1] == "join"
    assert record.output == {"tags": ["a2", "b1"]}


async def test_without_defer_join_runs_per_branch(service):
    record = await run(service, join_workflow(defer=False))
    assert record.status == "completed"
    assert trace_nodes(record).count("join") == 2


# 4-7 -------------------------------------------------------------------------------------------------
async def test_retry_then_success(service):
    key = uuid.uuid4().hex
    raw = wf(
        [
            {
                "id": "f",
                "type": "function",
                "function": "test.flaky",
                "arguments": {"key": f"'{key}'", "fail_times": 2},
                "retry": {"max_attempts": 3, "backoff_s": 0},
            }
        ],  # fmt: skip
        chain("f"),
    )
    record = await run(service, raw)
    assert record.status == "completed"
    assert record.output == {"attempts": 3}


async def test_failure_reports_node(service):
    record = await run(service, wf([{"id": "f", "type": "function", "function": "test.boom"}], chain("f")))
    assert record.status == "failed"
    assert record.error == {"type": "ValueError", "node": "f", "message": "boom"}


async def test_on_error_continue(service):
    raw = wf(
        [
            {"id": "f", "type": "function", "function": "test.boom", "on_error": "continue"},
            {"id": "after", **T, "mapping": {"saw": "nodes.f.error", "kind": "nodes.f.error_type"}},
        ],
        chain("f", "after"),
    )
    record = await run(service, raw)
    assert record.status == "completed"
    assert record.output == {"saw": "boom", "kind": "ValueError"}
    assert record.errors[0]["node"] == "f"
    assert record.trace[0]["status"] == "error"


async def test_timeout(service):
    raw = wf([{"id": "s", "type": "function", "function": "test.sleep", "arguments": {"seconds": 2},
               "timeout_s": 0.1}], chain("s"))  # fmt: skip
    record = await run(service, raw)
    assert record.status == "failed"
    assert record.error["type"] == "TimeoutError"
    assert "timed out after 0.1s" in record.error["message"]


# 8 ---------------------------------------------------------------------------------------------------
async def test_unbounded_loop_hits_visit_limit(service):
    raw = wf(
        [{"id": "a", **T, "mapping": {"n": "'again'"}}],
        [
            {"source": "start", "target": "a"},
            {
                "source": "a",
                "target": "a",
                "condition": [{"field": "nodes.a.stop", "operator": "exists", "target": "end"}],
            },
        ],
        limits={"max_node_visits": 3},
    )
    record = await run(service, raw)
    assert record.status == "failed"
    assert record.error["type"] == "NodeLoopLimitError"
    assert "max_visits=3" in record.error["message"]


# 9 ---------------------------------------------------------------------------------------------------
async def test_write_output_false_keeps_previous_output(service):
    raw = wf(
        [
            {"id": "first", **T, "mapping": {"text": "'first'"}},
            {"id": "side", **T, "mapping": {"text": "'side'"}, "write_output": False},
        ],
        chain("first", "side"),
    )
    record = await run(service, raw)
    assert record.output == {"text": "first"}


# 10 --------------------------------------------------------------------------------------------------
async def test_human_interrupt_and_resume(service):
    raw = wf(
        [
            {"id": "ask", "type": "human", "prompt": "Approve {{ input }}?"},
            {"id": "done", **T, "mapping": {"decision": "nodes.ask.response", "for": "input"}},
        ],
        chain("ask", "done"),
    )
    paused = await run(service, raw, "order-42")
    assert paused.status == "interrupted"
    assert paused.interrupt == [{"node": "ask", "prompt": "Approve order-42?"}]

    resumed = await service.resume(paused.id, "yes")
    assert resumed.status == "completed"
    assert resumed.output == {"decision": "yes", "for": "order-42"}
    assert resumed.interrupt is None
    assert [t["node"] for t in resumed.trace] == ["ask", "done"]

    with pytest.raises(RunStateError):
        await service.resume(paused.id, "again")


async def test_approval_catalog_flow(service):
    import json

    from conftest import WORKFLOWS_DIR

    raw = json.loads((WORKFLOWS_DIR / "approval.json").read_text())
    first = await run(service, raw, "INV-1001 was charged twice")
    assert first.status == "interrupted" and "Draft:" in first.interrupt[0]["prompt"]
    second = await service.resume(first.id, "add a goodwill credit")
    assert second.status == "interrupted" and "Revised draft:" in second.interrupt[0]["prompt"]
    third = await service.resume(first.id, "approve")
    assert third.status == "completed"
    assert third.output["approved"] is True
    assert third.output["review_rounds"] == 2
    assert third.output["text"].startswith("Revised draft:")


# 11 --------------------------------------------------------------------------------------------------
async def test_multi_turn_thread(service):
    raw = wf(
        [
            {"id": "reply", "type": "llm", "model": "scripted"},
            {
                "id": "check",
                **T,
                "mapping": {
                    "first": "messages.0.content",
                    "reply": "nodes.reply.text",
                    "count": "messages.length",
                },
            },  # fmt: skip
        ],
        chain("reply", "check"),
    )
    one = await run(service, raw, "one")
    assert one.output["reply"] == "echo: one"
    two = await run(service, raw, "two", thread_id=one.thread_id)
    assert two.output["first"] == "one"
    assert two.output["reply"] == "echo: two"
    assert [t["node"] for t in two.trace] == ["reply", "check"]  # trace reset per turn


# 12-14 -----------------------------------------------------------------------------------------------
async def test_llm_tool_loop_with_function(service):
    raw = wf(
        [{"id": "calc", "type": "llm", "model": "m", "tools": {"functions": ["calculator"]}}],
        chain("calc"),
        models={
            "m": scripted(
                responses=[
                    {"tool_calls": [{"name": "calculator", "args": {"expression": "6 * 7"}}]},
                    "It is 42",
                ]
            )
        },  # fmt: skip
    )
    record = await run(service, raw, "what is six times seven?")
    assert record.status == "completed"
    assert record.output == {"text": "It is 42"}
    assert record.trace[0]["tool_calls"] == 1


async def test_tool_budget_exceeded(service):
    call = {"tool_calls": [{"name": "calculator", "args": {"expression": "1 + 1"}}]}
    raw = wf(
        [
            {
                "id": "calc",
                "type": "llm",
                "model": "m",
                "tools": {"functions": ["calculator"]},
                "max_tool_calls": 2,
            }
        ],
        chain("calc"),
        models={"m": scripted(responses=[call, call, call, "done anyway"])},
    )
    record = await run(service, raw, "loop")
    assert record.status == "completed"
    assert record.trace[0]["tool_calls"] == 2
    assert record.text == "done anyway"


async def test_structured_output_mismatch_and_recovery(service):
    node = {
        "id": "classify",
        "type": "llm",
        "model": "m",
        "output_schema": {
            "type": "object",
            "properties": {"category": {"type": "string"}},
            "required": ["category"],
        },
    }
    models = {"m": scripted(responses={"wrong": 1})}
    failed = await run(service, wf([node], chain("classify"), models=models))
    assert failed.status == "failed"
    assert "output_schema" in failed.error["message"]

    recovered = await run(
        service, wf([{**node, "recover_text_field": "category"}], chain("classify"), models=models)
    )
    assert recovered.status == "completed"
    assert recovered.output == {"category": '{"wrong": 1}'}


# 15 --------------------------------------------------------------------------------------------------
async def test_async_run_and_cancel(service):
    raw = wf(
        [{"id": "s", "type": "function", "function": "test.sleep", "arguments": {"seconds": 5}}], chain("s")
    )
    definition, _ = service.resolve(workflow=raw)
    record = await service.start(definition, "", wait=False)
    for _ in range(100):
        record = await service.get(record.id)
        if record.status == "running":
            break
        await asyncio.sleep(0.01)
    assert record.status == "running"
    cancelled = await service.cancel(record.id)
    assert cancelled.status == "cancelled"
    with pytest.raises(RunStateError):
        await service.cancel(record.id)


async def test_cancel_interrupted_run(service):
    raw = wf([{"id": "ask", "type": "human"}], chain("ask"))
    paused = await run(service, raw)
    assert paused.status == "interrupted"
    assert (await service.cancel(paused.id)).status == "cancelled"


# 16 --------------------------------------------------------------------------------------------------
async def test_stream_events(service):
    raw = wf(
        [{"id": "a", **T, "mapping": {"x": "'1'"}}, {"id": "b", **T, "mapping": {"y": "'2'"}}],
        chain("a", "b"),
    )
    definition, _ = service.resolve(workflow=raw)
    record, events = await service.stream(definition, "hi")
    seen = [e async for e in events]
    assert [(e["event"], e["data"].get("node")) for e in seen] == [
        ("run_started", None),
        ("node_completed", "a"),
        ("node_completed", "b"),
        ("run_completed", None),
    ]
    assert seen[1]["data"]["result"] == {"x": "1"}
    assert seen[-1]["data"]["output"] == {"y": "2"}
    assert all(e["run_id"] == record.id and e["thread_id"] == record.thread_id for e in seen)


async def test_resume_stream_events(service):
    raw = wf([{"id": "ask", "type": "human"}], chain("ask"))
    definition, _ = service.resolve(workflow=raw)
    record, events = await service.stream(definition, "hi")
    first = [e["event"] async for e in events]
    assert first == ["run_started", "interrupted"]
    _, events = await service.resume_stream(record.id, "ok")
    assert [e["event"] async for e in events] == ["run_started", "node_completed", "run_completed"]


# 17 --------------------------------------------------------------------------------------------------
async def test_graph_cache_reuse(service):
    raw = wf([{"id": "a", **T, "mapping": {"x": "'1'"}}], chain("a"))
    await run(service, raw)
    await run(service, raw)
    assert len(service.cache) == 1


# Resolution ------------------------------------------------------------------------------------------
async def test_resolve_requires_exactly_one_source(service):
    from maw.schema import WorkflowValidationError

    with pytest.raises(WorkflowValidationError, match="exactly one"):
        service.resolve()
    with pytest.raises(WorkflowValidationError, match="unknown workflow_id 'nope'"):
        service.resolve(workflow_id="nope")


# Catalog ---------------------------------------------------------------------------------------------
async def test_catalog_loads_files_and_registers_api_workflows(tmp_path):
    from contextlib import AsyncExitStack

    from conftest import build_service, make_settings

    from maw.schema import WorkflowValidationError

    connections = tmp_path / "connections.yaml"
    connections.write_text(
        "mcp_servers:\n  kb: {url: 'http://localhost:1/mcp'}\nagents:\n  billing: {url: 'http://localhost:2'}\n"
    )
    (tmp_path / "wfs").mkdir()
    (tmp_path / "wfs" / "named_by_file.json").write_text('{"nodes": [{"id": "a", "type": "transform", "mapping": {}}], '
                                                         '"edges": [{"source": "start", "target": "a"}, {"source": "a", "target": "end"}]}')  # fmt: skip
    (tmp_path / "wfs" / "broken.json").write_text('{"nodes": [], "edges": []}')
    (tmp_path / "wfs" / "not_a_workflow.json").write_text('{"hello": 1}')
    (tmp_path / "wfs" / "garbage.json").write_text("{nope")

    async with AsyncExitStack() as stack:
        svc = await build_service(make_settings(tmp_path, connections_file=connections), stack)
        ids = [e.workflow.id for e in svc.catalog.entries()]
        assert ids == ["approval", "concierge", "hello", "kb_research", "support_triage"]
        assert [e.workflow.id for e in svc.catalog.exposed()] == ["concierge", "hello", "support_triage"]

        other = await build_service(make_settings(tmp_path, workflows_dir=tmp_path / "wfs"), stack)
        assert [e.workflow.id for e in other.catalog.entries()] == ["named_by_file"]  # id from the file stem

        api = wf([{"id": "a", **T, "mapping": {"x": "'1'"}}], chain("a"), id="from_api")
        entry = await svc.catalog.register(api)
        assert entry.source == "api" and svc.catalog.get("from_api") is entry
        with pytest.raises(WorkflowValidationError, match="defined in a file"):
            await svc.catalog.register({**api, "id": "hello"})
        with pytest.raises(WorkflowValidationError, match="defined in a file"):
            await svc.catalog.delete("hello")
        assert await svc.catalog.delete("from_api") is True
        assert await svc.catalog.delete("from_api") is False

        record = await svc.start(svc.resolve(workflow_id="hello")[0], "hey")
        assert record.text == "echo: hey"
