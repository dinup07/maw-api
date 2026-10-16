"""RunService: the one execution path shared by REST (sync/async/SSE) and the A2A server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from ..engine.compiler import GraphCache
from ..engine.nodes import EngineContext, NodeExecutionError, NodeLoopLimitError
from ..engine.paths import to_text
from ..engine.state import new_turn_input
from ..functions import function_names
from ..observability import metrics
from ..observability.logging import bind, run_id_var, workflow_id_var
from ..observability.tracing import span
from ..schema import WorkflowDefinition, WorkflowValidationError, parse_workflow, validate_graph
from ..settings import Settings
from .catalog import Catalog
from .persistence import Persistence
from .store import TERMINAL, RunRecord

log = logging.getLogger("maw.runtime")

Event = dict[str, Any]
_DONE = object()  # end-of-stream sentinel for subscriber queues


class RunNotFound(LookupError):
    pass


class RunStateError(RuntimeError):
    pass


def json_safe(value: Any) -> Any:
    """Make a value JSON-serializable; non-JSON leaves (e.g. messages) become text."""
    return json.loads(json.dumps(value, default=to_text))


class RunService:
    def __init__(self, settings: Settings, ctx: EngineContext, persistence: Persistence, catalog: Catalog):
        self.settings = settings
        self.ctx = ctx
        self.persistence = persistence
        self.catalog = catalog
        self.cache = GraphCache(settings.graph_cache_size)
        self._tasks: dict[str, asyncio.Task] = {}
        # Runs whose execution hasn't reached its final save. A task leaves this set slightly
        # before it finishes, so a client reacting to the end of a stream can resume at once.
        self._active: set[str] = set()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._thread_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sem = asyncio.Semaphore(settings.max_concurrent_runs)
        self._shutting_down = False

    @property
    def store(self):
        return self.persistence.store

    @property
    def durable(self) -> bool:
        return self.persistence.durable

    # ---------------------------------------------------------------------------------------------
    # Resolution and validation
    # ---------------------------------------------------------------------------------------------

    def validate(self, wf: WorkflowDefinition) -> None:
        validate_graph(
            wf,
            known_mcp_servers=self.ctx.connections.mcp_servers,
            known_agents=self.ctx.connections.agents,
            known_functions=function_names(),
            known_vector_stores=self.ctx.connections.vector_stores,
        )

    def resolve(
        self, workflow: dict[str, Any] | None = None, workflow_id: str | None = None
    ) -> tuple[WorkflowDefinition, list[str]]:
        if (workflow is None) == (workflow_id is None):
            raise WorkflowValidationError(["provide exactly one of 'workflow' or 'workflow_id'"])
        if workflow_id is not None:
            entry = self.catalog.get(workflow_id)
            if entry is None:
                raise WorkflowValidationError([f"unknown workflow_id '{workflow_id}'"])
            return entry.workflow, list(entry.warnings)
        wf, warnings = parse_workflow(workflow)
        self.validate(wf)
        return wf, warnings

    # ---------------------------------------------------------------------------------------------
    # Starting, resuming, cancelling
    # ---------------------------------------------------------------------------------------------

    async def create_run(self, wf: WorkflowDefinition, input: Any, thread_id: str | None = None) -> RunRecord:
        run = RunRecord(
            workflow_id=wf.id,
            workflow_hash=wf.content_hash(),
            thread_id=thread_id or "thr_" + uuid4().hex[:16],
            input=input,
            definition=wf.model_dump(mode="json"),
        )
        await self.store.save_run(run)
        return run

    def launch(self, run: RunRecord, wf: WorkflowDefinition, payload: Any) -> asyncio.Task:
        task = asyncio.create_task(self._execute(run, wf, payload), name=run.id)
        self._tasks[run.id] = task
        self._active.add(run.id)

        def _forget(t: asyncio.Task, run_id: str = run.id) -> None:
            if self._tasks.get(run_id) is t:
                del self._tasks[run_id]

        task.add_done_callback(_forget)
        return task

    async def start(
        self, wf: WorkflowDefinition, input: Any, *, thread_id: str | None = None, wait: bool = True
    ) -> RunRecord:
        run = await self.create_run(wf, input, thread_id)
        task = self.launch(run, wf, new_turn_input(input))
        if wait:
            await self._wait(task)
        return await self.get(run.id)

    async def stream(
        self, wf: WorkflowDefinition, input: Any, *, thread_id: str | None = None
    ) -> tuple[RunRecord, AsyncIterator[Event]]:
        run = await self.create_run(wf, input, thread_id)
        queue = self._subscribe(run.id)  # before launching, so no event is lost
        self.launch(run, wf, new_turn_input(input))
        return run, self._iterate(run.id, queue)

    async def _prepare_resume(self, run_id: str) -> tuple[RunRecord, WorkflowDefinition]:
        run = await self.get(run_id)
        if run.status != "interrupted" or run_id in self._active:
            state = "running" if run_id in self._active else run.status
            raise RunStateError(f"run is {state}, not interrupted")
        wf, _ = parse_workflow(run.definition)  # the definition stored on the run, not the catalog's
        return run, wf

    async def resume(self, run_id: str, value: Any, *, wait: bool = True) -> RunRecord:
        run, wf = await self._prepare_resume(run_id)
        task = self.launch(run, wf, Command(resume=value))
        if wait:
            await self._wait(task)
        return await self.get(run_id)

    async def resume_stream(self, run_id: str, value: Any) -> tuple[RunRecord, AsyncIterator[Event]]:
        run, wf = await self._prepare_resume(run_id)
        queue = self._subscribe(run_id)
        self.launch(run, wf, Command(resume=value))
        return run, self._iterate(run_id, queue)

    async def cancel(self, run_id: str) -> RunRecord:
        run = await self.get(run_id)
        if run.status in TERMINAL:
            raise RunStateError(f"run is already {run.status}")
        task = self._tasks.get(run_id) if run_id in self._active else None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        else:  # interrupted (or queued with no live task, e.g. after a restart)
            run.status = "cancelled"
            run.interrupt = None
            await self.store.save_run(run)
        return await self.get(run_id)

    async def get(self, run_id: str) -> RunRecord:
        run = await self.store.get_run(run_id)
        if run is None:
            raise RunNotFound(f"run '{run_id}' not found")
        return run

    async def list_runs(
        self, *, status: str | None = None, workflow_id: str | None = None, limit: int = 50
    ) -> list[RunRecord]:
        return await self.store.list_runs(status=status, workflow_id=workflow_id, limit=limit)

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def recover(self) -> int:
        """Relaunch runs left ``running``/``queued`` by a previous process (durable stores only)."""
        if not (self.durable and self.settings.resume_on_startup):
            return 0
        count = 0
        for status in ("running", "queued"):
            for run in await self.store.list_runs(status=status, limit=10_000):
                if run.id in self._active:
                    continue
                try:
                    wf, _ = parse_workflow(run.definition)
                    graph, _ = self.cache.get_or_compile(wf, self.ctx, self.persistence.checkpointer)
                    snap = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
                    payload = None if snap.values else new_turn_input(run.input)
                    log.info(
                        "recovering run",
                        extra={"data": {"run_id": run.id, "from_checkpoint": payload is None}},
                    )
                    self.launch(run, wf, payload)
                    count += 1
                except Exception as e:
                    log.exception("could not recover run", extra={"data": {"run_id": run.id}})
                    run.status = "failed"
                    run.error = {"type": "RecoveryError", "message": f"{type(e).__name__}: {e}"}
                    await self.store.save_run(run)
        return count

    # ---------------------------------------------------------------------------------------------
    # Events
    # ---------------------------------------------------------------------------------------------

    def _subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    async def _iterate(self, run_id: str, queue: asyncio.Queue) -> AsyncIterator[Event]:
        try:
            while True:
                event = await queue.get()
                if event is _DONE:
                    return
                yield event
        finally:
            subs = self._subscribers.get(run_id)
            if subs and queue in subs:
                subs.remove(queue)

    def _emit(self, run: RunRecord, name: str, data: dict[str, Any]) -> None:
        subs = self._subscribers.get(run.id)
        if not subs:
            return
        event = {"event": name, "run_id": run.id, "thread_id": run.thread_id, "data": json_safe(data)}
        for queue in subs:
            queue.put_nowait(event)

    # ---------------------------------------------------------------------------------------------
    # Execution
    # ---------------------------------------------------------------------------------------------

    async def _wait(self, task: asyncio.Task) -> None:
        """Wait for a run. If *this* waiter is cancelled (client disconnect) the run keeps going."""
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # we were cancelled; the shielded run continues and records itself
            # otherwise the run itself was cancelled; its record says so

    async def _execute(self, run: RunRecord, wf: WorkflowDefinition, payload: Any) -> None:
        started = time.perf_counter()
        with bind(run_id_var, run.id), bind(workflow_id_var, wf.id):
            try:
                async with self._sem, self._thread_locks[run.thread_id]:
                    graph, _ = self.cache.get_or_compile(wf, self.ctx, self.persistence.checkpointer)
                    config = {
                        "configurable": {"thread_id": run.thread_id},
                        "recursion_limit": wf.limits.recursion_limit or self.settings.default_recursion_limit,
                        "run_name": f"workflow:{wf.id}",
                        "metadata": {
                            "run_id": run.id,
                            "workflow_id": wf.id,
                            "workflow_hash": run.workflow_hash,
                        },
                    }
                    run.status, run.error, run.interrupt = "running", None, None
                    await self.store.save_run(run)
                    self._emit(run, "run_started", {"workflow_id": wf.id})
                    metrics.runs_in_flight.inc()
                    try:
                        with span("workflow.run", workflow_id=wf.id, run_id=run.id):
                            async for chunk in graph.astream(payload, config, stream_mode="updates"):
                                for node_id, update in chunk.items():
                                    if node_id.startswith("__") or not isinstance(update, dict):
                                        continue
                                    trace = (update.get("trace") or [{}])[0]
                                    self._emit(
                                        run,
                                        "node_completed",
                                        {
                                            "node": node_id,
                                            "status": trace.get("status"),
                                            "ms": trace.get("ms"),
                                            "result": (update.get("nodes") or {}).get(node_id),
                                        },
                                    )
                            snap = await graph.aget_state(config)
                    finally:
                        metrics.runs_in_flight.dec()

                values = snap.values or {}
                run.output = json_safe(values.get("output"))
                run.trace = json_safe(values.get("trace") or [])
                run.errors = json_safe(values.get("errors") or [])
                interrupts = [i.value for t in snap.tasks for i in t.interrupts]
                if interrupts:
                    run.status, run.interrupt = "interrupted", json_safe(interrupts)
                    self._emit(run, "interrupted", {"interrupt": run.interrupt})
                else:
                    run.status = "completed"
                    self._emit(run, "run_completed", {"output": run.output})
            except asyncio.CancelledError:
                if self._shutting_down and self.durable:
                    run.status = "running"  # left for recover() on the next start
                else:
                    run.status = "cancelled"
                    self._emit(run, "run_cancelled", {})
                raise
            except NodeExecutionError as e:
                run.status = "failed"
                run.error = {"type": type(e.cause).__name__, "node": e.node_id, "message": str(e.cause)}
            except (GraphRecursionError, NodeLoopLimitError) as e:
                run.status = "failed"
                run.error = {"type": type(e).__name__, "message": str(e).splitlines()[0] if str(e) else ""}
            except Exception as e:
                log.exception("run failed")
                run.status = "failed"
                run.error = {"type": type(e).__name__, "message": str(e)}
            finally:
                elapsed = time.perf_counter() - started
                run.duration_ms = round(elapsed * 1000, 2)
                if run.status == "failed":
                    self._emit(run, "run_failed", {"error": run.error})
                if run.status in TERMINAL or run.status == "interrupted":
                    metrics.runs_total.labels(wf.id, run.status).inc()
                    metrics.run_duration_seconds.labels(wf.id).observe(elapsed)
                # Detach this execution's subscribers before saving, so a resume that subscribes
                # as soon as the new status is visible never has its queue closed by us.
                subscribers = self._subscribers.pop(run.id, [])
                self._active.discard(run.id)
                await asyncio.shield(self.store.save_run(run))
                for queue in subscribers:
                    queue.put_nowait(_DONE)
                log.info(
                    "run finished",
                    extra={
                        "data": {"status": run.status, "duration_ms": run.duration_ms, "error": run.error}
                    },
                )
