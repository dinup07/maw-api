"""Durability on Postgres. Each test builds a second service on the same database to simulate a restart.

Skipped unless MAW_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest
from conftest import build_service, chain, make_settings, wf

CLEAN_TABLES = ("maw_runs", "maw_workflows", "checkpoints", "checkpoint_blobs", "checkpoint_writes")


@pytest.fixture
async def durable(tmp_path, database_url):
    """Factory for services on a clean database; each call is a fresh 'process'."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        for table in CLEAN_TABLES:
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS checkpoint_migrations CASCADE")

    stacks: list[AsyncExitStack] = []

    async def start():
        stack = AsyncExitStack()
        stacks.append(stack)
        service = await build_service(
            make_settings(tmp_path, workflows_dir=None, database_url=database_url), stack
        )
        assert service.durable
        return service, stack

    yield start
    for stack in reversed(stacks):
        await stack.aclose()


async def wait_for(service, run_id, statuses):
    for _ in range(300):  # up to ~15 s
        record = await service.get(run_id)
        if record.status in statuses:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} stayed {record.status}")


async def test_paused_run_resumes_after_restart(durable):
    raw = wf(
        [
            {"id": "ask", "type": "human", "prompt": "Approve {{ input.order }}?"},
            {
                "id": "done",
                "type": "transform",
                "mapping": {"decision": "nodes.ask.response", "original": "input"},
            },
        ],
        chain("ask", "done"),
    )
    first, stack = await durable()
    definition, _ = first.resolve(workflow=raw)
    paused = await first.start(definition, {"order": "order-42"})
    assert paused.status == "interrupted"
    assert paused.interrupt == [{"node": "ask", "prompt": "Approve order-42?"}]
    await stack.aclose()  # "process" exits

    second, _ = await durable()
    assert (await second.get(paused.id)).status == "interrupted"
    resumed = await second.resume(paused.id, "yes")
    assert resumed.status == "completed"
    assert resumed.output == {"decision": "yes", "original": {"order": "order-42"}}
    assert [t["node"] for t in resumed.trace] == ["ask", "done"]


async def test_in_flight_run_recovered_from_checkpoint(durable):
    raw = wf(
        [
            {
                "id": "one",
                "type": "function",
                "function": "test.sleep",
                "arguments": {"seconds": 0.01, "tag": "'one'"},
            },
            {
                "id": "two",
                "type": "function",
                "function": "test.sleep",
                "arguments": {"seconds": 2, "tag": "'two'"},
            },
            {
                "id": "three",
                "type": "function",
                "function": "test.sleep",
                "arguments": {"seconds": 0.01, "tag": "'three'"},
            },
        ],
        chain("one", "two", "three"),
    )
    first, stack = await durable()
    definition, _ = first.resolve(workflow=raw)
    started = await first.start(definition, "go", wait=False)
    await asyncio.sleep(0.5)  # step one done and checkpointed; step two in flight
    await stack.aclose()  # shutdown with a durable store leaves the run recoverable

    second, _ = await durable()
    assert (await second.get(started.id)).status == "running"
    assert await second.recover() >= 1
    record = await wait_for(second, started.id, {"completed", "failed"})
    assert record.status == "completed", record.error
    assert [t["node"] for t in record.trace] == ["one", "two", "three"]  # step one was not re-run
    assert record.output == {"slept": 0.01, "tag": "three"}


async def test_queued_run_without_checkpoint_restarts_fresh(durable):
    first, stack = await durable()
    definition, _ = first.resolve(
        workflow=wf([{"id": "a", "type": "transform", "mapping": {"x": "input"}}], chain("a"))
    )
    queued = await first.create_run(definition, "hello")  # never launched: no checkpoint
    await stack.aclose()

    second, _ = await durable()
    assert await second.recover() == 1
    record = await wait_for(second, queued.id, {"completed", "failed"})
    assert record.status == "completed" and record.output == {"x": "hello"}


async def test_api_workflows_persist_across_restart(durable):
    raw = wf([{"id": "a", "type": "transform", "mapping": {"text": "input"}}], chain("a"), id="persisted_wf")
    first, stack = await durable()
    await first.catalog.register(raw)
    await stack.aclose()

    second, stack = await durable()
    entry = second.catalog.get("persisted_wf")
    assert entry is not None and entry.source == "api"
    record = await second.start(entry.workflow, "still here")
    assert record.text == "still here"
    assert await second.catalog.delete("persisted_wf") is True
    await stack.aclose()

    third, _ = await durable()
    assert third.catalog.get("persisted_wf") is None


async def test_run_history_survives_restart(durable):
    first, stack = await durable()
    definition, _ = first.resolve(
        workflow=wf([{"id": "a", "type": "transform", "mapping": {"x": "'1'"}}], chain("a"))
    )
    done = await first.start(definition, "x")
    await stack.aclose()

    second, _ = await durable()
    listed = await second.list_runs(status="completed")
    assert [r.id for r in listed] == [done.id]
    assert listed[0].definition["nodes"][0]["id"] == "a"  # definition re-attached from its own column
