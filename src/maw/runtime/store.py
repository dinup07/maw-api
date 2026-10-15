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


# --------------------------------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------------------------------

# psycopg 3 runs one statement per execute() (spec trap T11), so the schema is a list.
POSTGRES_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS maw_runs (
        id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, status TEXT NOT NULL, thread_id TEXT NOT NULL,
        record JSONB NOT NULL, definition JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS maw_runs_status_idx ON maw_runs (status)",
    "CREATE INDEX IF NOT EXISTS maw_runs_workflow_idx ON maw_runs (workflow_id, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS maw_workflows (
        id TEXT PRIMARY KEY, hash TEXT NOT NULL, definition JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
)


class PostgresStore:
    """Run records and API-registered workflows in Postgres. Parameterized queries only.

    The pool must be created with ``row_factory=dict_row``.
    """

    def __init__(self, pool: Any):
        self.pool = pool

    async def setup(self) -> None:
        async with self.pool.connection() as conn:
            for statement in POSTGRES_SCHEMA:
                await conn.execute(statement)

    async def close(self) -> None:
        return None  # the pool is owned (and closed) by open_persistence

    async def save_run(self, run: RunRecord) -> None:
        from psycopg.types.json import Jsonb

        run.updated_at = utcnow()
        record = run.model_dump(mode="json")  # `definition` is excluded; it has its own column
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO maw_runs (id, workflow_id, status, thread_id, record, definition, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE
                   SET status = EXCLUDED.status, record = EXCLUDED.record, updated_at = EXCLUDED.updated_at""",
                (
                    run.id,
                    run.workflow_id,
                    run.status,
                    run.thread_id,
                    Jsonb(record),
                    Jsonb(run.definition),
                    run.created_at,
                    run.updated_at,
                ),
            )

    @staticmethod
    def _run(row: dict[str, Any]) -> RunRecord:
        return RunRecord.model_validate({**row["record"], "definition": row["definition"]})

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT record, definition FROM maw_runs WHERE id = %s", (run_id,))
            row = await cur.fetchone()
        return self._run(row) if row else None

    async def list_runs(
        self, *, status: str | None = None, workflow_id: str | None = None, limit: int = 50
    ) -> list[RunRecord]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if workflow_id is not None:
            clauses.append("workflow_id = %s")
            params.append(workflow_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT record, definition FROM maw_runs {where} ORDER BY created_at DESC LIMIT %s",
                (*params, limit),
            )
            rows = await cur.fetchall()
        return [self._run(row) for row in rows]

    async def save_workflow(self, wf: StoredWorkflow) -> None:
        from psycopg.types.json import Jsonb

        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO maw_workflows (id, hash, definition, updated_at) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE
                   SET hash = EXCLUDED.hash, definition = EXCLUDED.definition, updated_at = EXCLUDED.updated_at""",
                (wf.id, wf.hash, Jsonb(wf.definition), wf.updated_at),
            )

    async def list_workflows(self) -> list[StoredWorkflow]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT id, hash, definition, updated_at FROM maw_workflows ORDER BY id")
            rows = await cur.fetchall()
        return [StoredWorkflow(source="api", **row) for row in rows]

    async def delete_workflow(self, workflow_id: str) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute("DELETE FROM maw_workflows WHERE id = %s", (workflow_id,))
            return cur.rowcount > 0
