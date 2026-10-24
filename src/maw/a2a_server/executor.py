"""Run a MAW workflow as an A2A agent, through the same RunService as REST.

A2A ``context_id`` is the MAW thread id; a ``human`` pause maps to ``input_required`` and the next
message on that task resumes the run.
"""

from __future__ import annotations

import logging
from typing import Any

from a2a.helpers import (
    get_data_parts,
    get_text_parts,
    new_data_part,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, TaskState

from ..engine.paths import to_text
from ..runtime import RunNotFound, RunService, RunStateError
from ..schema import WorkflowDefinition, WorkflowValidationError

log = logging.getLogger("maw.a2a_server")


def input_from_message(message: Message | None) -> Any:
    if message is None:
        return ""
    text = "\n".join(get_text_parts(message.parts))
    data = get_data_parts(message.parts)
    if not data:
        return text
    if len(data) == 1 and isinstance(data[0], dict):
        return {"text": text, **data[0]} if text else data[0]
    return {"text": text, "data": data}


def output_parts(output: Any) -> list[Part]:
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        text = output["text"]
    else:
        text = to_text(output)
    parts = [new_text_part(text)]
    if isinstance(output, dict) and set(output) - {"text"}:
        parts.append(new_data_part(output))
    return parts


class WorkflowAgentExecutor(AgentExecutor):
    def __init__(self, service: RunService, workflow: WorkflowDefinition):
        self.service = service
        self.workflow_id = workflow.id
        self._fallback = workflow
        self._runs_by_task: dict[str, str] = {}

    def _workflow(self) -> WorkflowDefinition | None:
        """The catalog's current definition (it may have been re-registered since mounting)."""
        entry = self.service.catalog.get(self.workflow_id)
        return entry.workflow if entry else None

    async def _find_paused_run(self, context_id: str) -> str | None:
        runs = await self.service.list_runs(status="interrupted", workflow_id=self.workflow_id, limit=500)
        for run in runs:  # newest first
            if run.thread_id == context_id:
                return run.id
        return None

    async def execute(self, context: RequestContext, event_queue: Any) -> None:
        task = context.current_task
        resuming = task is not None and task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        user_input = input_from_message(context.message)

        def agent_text(text: str) -> Message:
            return updater.new_agent_message([new_text_part(text)])

        try:
            run_id = None
            if resuming:
                run_id = self._runs_by_task.get(task.id) or await self._find_paused_run(task.context_id)
            if run_id is not None:
                run, events = await self.service.resume_stream(run_id, user_input)
            else:
                workflow = self._workflow()
                if workflow is None:
                    await updater.failed(agent_text(f"Workflow '{self.workflow_id}' is no longer available"))
                    return
                run, events = await self.service.stream(workflow, user_input, thread_id=task.context_id)
        except (WorkflowValidationError, RunStateError, RunNotFound) as e:
            await updater.failed(agent_text(f"Workflow failed: {e}"))
            return

        self._runs_by_task[task.id] = run.id
        await updater.start_work()
        async for event in events:
            name, data = event["event"], event["data"]
            if name == "node_completed":
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=agent_text(f"Completed step '{data.get('node')}'"),
                    metadata={"maw_run_id": run.id, "node": data.get("node")},
                )
            elif name == "interrupted":
                prompts = [str(i.get("prompt")) for i in data.get("interrupt") or [] if isinstance(i, dict)]
                await updater.requires_input(
                    agent_text("\n\n".join(p for p in prompts if p) or "Input required")
                )
                return
            elif name == "run_completed":
                await updater.add_artifact(output_parts(data.get("output")), name="result")
                await updater.complete()
                return
            elif name == "run_failed":
                error = data.get("error") or {}
                await updater.failed(agent_text(f"Workflow failed: {error.get('message', 'unknown error')}"))
                return
            elif name == "run_cancelled":
                await updater.cancel()
                return

        # The stream ended without a terminal event: fall back to the stored record.
        record = await self.service.get(run.id)
        if record.status == "completed":
            await updater.add_artifact(output_parts(record.output), name="result")
            await updater.complete()
        else:
            message = (record.error or {}).get("message", f"run ended as {record.status}")
            await updater.failed(agent_text(f"Workflow failed: {message}"))

    async def cancel(self, context: RequestContext, event_queue: Any) -> None:
        run_id = self._runs_by_task.get(context.task_id or "")
        if run_id is not None:
            try:
                await self.service.cancel(run_id)
            except Exception as e:
                log.warning("could not cancel run", extra={"data": {"run_id": run_id, "error": str(e)}})
        task = context.current_task
        if task is not None:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()
