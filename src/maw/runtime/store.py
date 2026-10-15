"""Run records, stored workflows and the stores that persist them."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

RunStatus = Literal["queued", "running", "interrupted", "completed", "failed", "cancelled"]
TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunRecord(BaseModel):
    id: str = Field(default_factory=lambda: "run_" + uuid4().hex[:16])
    workflow_id: str
    workflow_hash: str
    thread_id: str
    status: RunStatus = "queued"
    input: Any = None
    output: Any = None
    error: dict[str, Any] | None = None  # {"type", "message", "node"?}
    interrupt: list[Any] | None = None  # e.g. [{"node", "prompt"}]
    trace: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    definition: dict[str, Any] = Field(default_factory=dict, exclude=True)  # stored, never serialized
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    duration_ms: float | None = None

    @property
    def text(self) -> str | None:
        if isinstance(self.output, dict) and isinstance(self.output.get("text"), str):
            return self.output["text"]
        return self.output if isinstance(self.output, str) else None


class StoredWorkflow(BaseModel):
    id: str
    hash: str
    definition: dict[str, Any]
    source: Literal["file", "api"]
    updated_at: datetime = Field(default_factory=utcnow)


class Store(Protocol):
    async def setup(self) -> None: ...
    async def close(self) -> None: ...
    async def save_run(self, run: RunRecord) -> None: ...
    async def get_run(self, run_id: str) -> RunRecord | None: ...
    async def list_runs(
        self, *, status: str | None = None, workflow_id: str | None = None, limit: int = 50
    ) -> list[RunRecord]: ...
    async def save_workflow(self, wf: StoredWorkflow) -> None: ...
    async def list_workflows(self) -> list[StoredWorkflow]: ...
    async def delete_workflow(self, workflow_id: str) -> bool: ...


def _copy_run(run: RunRecord) -> RunRecord:
    # model_copy(deep=True) keeps the excluded `definition` field, unlike a dump/validate round-trip.
    return run.model_copy(deep=True)


class MemoryStore:
    """In-process store. Returns deep copies so callers can't mutate stored state."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._workflows: dict[str, StoredWorkflow] = {}

    async def setup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def save_run(self, run: RunRecord) -> None:
        run.updated_at = utcnow()
        self._runs[run.id] = _copy_run(run)

    async def get_run(self, run_id: str) -> RunRecord | None:
        run = self._runs.get(run_id)
        return _copy_run(run) if run else None

    async def list_runs(
        self, *, status: str | None = None, workflow_id: str | None = None, limit: int = 50
    ) -> list[RunRecord]:
        runs = [
            r
            for r in self._runs.values()
            if (status is None or r.status == status)
            and (workflow_id is None or r.workflow_id == workflow_id)
        ]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return [_copy_run(r) for r in runs[:limit]]

    async def save_workflow(self, wf: StoredWorkflow) -> None:
        self._workflows[wf.id] = wf.model_copy(deep=True)

    async def list_workflows(self) -> list[StoredWorkflow]:
        return [copy.deepcopy(w) for w in self._workflows.values()]

    async def delete_workflow(self, workflow_id: str) -> bool:
        return self._workflows.pop(workflow_id, None) is not None
