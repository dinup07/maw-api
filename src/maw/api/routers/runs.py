"""Runs: start (sync, async, SSE), list, get, cancel, resume."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, model_validator

from ...runtime import RunRecord, RunService
from ...runtime.store import RunStatus
from ..deps import get_service

router = APIRouter(prefix="/v1/runs", tags=["runs"])


class RunRequest(BaseModel):
    workflow: dict[str, Any] | None = None  # inline definition (native or reference format)
    workflow_id: str | None = None  # catalog id
    input: Any = ""
    thread_id: str | None = None  # reuse to continue a conversation
    mode: Literal["sync", "async"] = "sync"

    @model_validator(mode="before")
    @classmethod
    def _reference_request(cls, data: Any) -> Any:
        """Accept the reference request shape: workflow_config / input_prompt / session_id / async."""
        if not isinstance(data, dict) or "workflow_config" not in data:
            return data
        data = dict(data)
        workflow = {"workflow_config": data.pop("workflow_config")}
        if "config" in data:
            workflow["config"] = data.pop("config")
        out: dict[str, Any] = {"workflow": workflow}
        if "input_prompt" in data:
            out["input"] = data.pop("input_prompt")
        if "session_id" in data:
            out["thread_id"] = data.pop("session_id")
        if "async" in data:
            out["mode"] = "async" if str(data.pop("async")).lower() == "true" else "sync"
        data.pop("user_id", None)
        out.update(data)  # any native fields alongside
        return out


class ResumeRequest(BaseModel):
    value: Any = None
    mode: Literal["sync", "async"] = "sync"


class RunResponse(BaseModel):
    id: str
    workflow_id: str
    workflow_hash: str
    thread_id: str
    status: RunStatus
    output: Any = None
    text: str | None = None
    error: dict[str, Any] | None = None
    interrupt: list[Any] | None = None
    errors: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    warnings: list[str] = []
    duration_ms: float | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, run: RunRecord, warnings: list[str] | None = None) -> RunResponse:
        return cls(**run.model_dump(), text=run.text, warnings=warnings or [])


def _sse(name: str, data: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream_response(first: dict[str, Any], events: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        yield _sse(first["event"], first)
        async for event in events:
            yield _sse(event["event"], event)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("", response_model=RunResponse)
async def start_run(req: RunRequest, service: RunService = Depends(get_service)) -> Any:
    wf, warnings = service.resolve(req.workflow, req.workflow_id)
    run = await service.start(wf, req.input, thread_id=req.thread_id, wait=req.mode == "sync")
    body = RunResponse.of(run, warnings)
    if req.mode == "async":
        return JSONResponse(status_code=202, content=body.model_dump(mode="json"))
    return body


@router.post("/stream")
async def stream_run(req: RunRequest, service: RunService = Depends(get_service)) -> StreamingResponse:
    wf, warnings = service.resolve(req.workflow, req.workflow_id)
    run, events = await service.stream(wf, req.input, thread_id=req.thread_id)
    created = {
        "event": "run_created",
        "run_id": run.id,
        "thread_id": run.thread_id,
        "data": {"warnings": warnings},
    }
    return _stream_response(created, events)


@router.get("", response_model=list[RunResponse])
async def list_runs(
    status: RunStatus | None = None,
    workflow_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    service: RunService = Depends(get_service),
) -> list[RunResponse]:
    runs = await service.list_runs(status=status, workflow_id=workflow_id, limit=limit)
    return [RunResponse.of(r) for r in runs]


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, service: RunService = Depends(get_service)) -> RunResponse:
    return RunResponse.of(await service.get(run_id))


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: str, service: RunService = Depends(get_service)) -> RunResponse:
    return RunResponse.of(await service.cancel(run_id))


@router.post("/{run_id}/resume", response_model=RunResponse)
async def resume_run(run_id: str, req: ResumeRequest, service: RunService = Depends(get_service)) -> Any:
    run = await service.resume(run_id, req.value, wait=req.mode == "sync")
    if req.mode == "async":
        return JSONResponse(status_code=202, content=RunResponse.of(run).model_dump(mode="json"))
    return RunResponse.of(run)


@router.post("/{run_id}/resume/stream")
async def resume_run_stream(
    run_id: str, req: ResumeRequest, service: RunService = Depends(get_service)
) -> StreamingResponse:
    run, events = await service.resume_stream(run_id, req.value)
    resumed = {"event": "run_resumed", "run_id": run.id, "thread_id": run.thread_id, "data": {}}
    return _stream_response(resumed, events)
