"""Workflow catalog, validation and the JSON Schema of the workflow contract."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ...a2a_server import agent_base_url
from ...engine.compiler import compile_workflow
from ...runtime import CatalogEntry, RunService
from ...schema import WorkflowDefinition, WorkflowValidationError, parse_workflow
from ..deps import get_service

router = APIRouter(tags=["workflows"])


class WorkflowSummary(BaseModel):
    id: str
    hash: str
    source: str
    description: str | None = None
    node_count: int
    a2a_url: str | None = None


class RegisterRequest(BaseModel):
    workflow: dict[str, Any]


def summarize(entry: CatalogEntry, public_url: str) -> WorkflowSummary:
    wf = entry.workflow
    return WorkflowSummary(
        id=wf.id,
        hash=entry.hash,
        source=entry.source,
        description=wf.description,
        node_count=len(wf.nodes),
        a2a_url=agent_base_url(public_url, wf.id) if wf.expose.a2a else None,
    )


def mermaid(wf: WorkflowDefinition, service: RunService) -> str:
    return compile_workflow(wf, service.ctx).get_graph().draw_mermaid()


@router.get("/v1/schema")
async def workflow_schema() -> dict[str, Any]:
    return WorkflowDefinition.model_json_schema()


@router.post("/v1/workflows/validate")
async def validate_workflow(
    raw: dict[str, Any], service: RunService = Depends(get_service)
) -> dict[str, Any]:
    try:
        wf, warnings = parse_workflow(raw)
        service.validate(wf)
    except WorkflowValidationError as e:
        return {"valid": False, "errors": e.errors, "warnings": []}
    return {
        "valid": True,
        "errors": [],
        "warnings": warnings,
        "hash": wf.content_hash(),
        "normalized": wf.model_dump(mode="json", exclude_defaults=True),
        "mermaid": mermaid(wf, service),
    }


@router.get("/v1/workflows", response_model=list[WorkflowSummary])
async def list_workflows(service: RunService = Depends(get_service)) -> list[WorkflowSummary]:
    return [summarize(e, service.settings.public_url) for e in service.catalog.entries()]


@router.get("/v1/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    entry = service.catalog.get(workflow_id)
    if entry is None:
        raise HTTPException(404, f"workflow '{workflow_id}' not found")
    return {
        "summary": summarize(entry, service.settings.public_url).model_dump(),
        "definition": entry.workflow.model_dump(mode="json", exclude_defaults=True),
        "mermaid": mermaid(entry.workflow, service),
    }


@router.post("/v1/workflows", status_code=201, response_model=WorkflowSummary)
async def register_workflow(
    req: RegisterRequest, request: Request, service: RunService = Depends(get_service)
) -> WorkflowSummary:
    if not service.settings.allow_workflow_registration:
        raise HTTPException(403, "workflow registration is disabled (MAW_ALLOW_WORKFLOW_REGISTRATION)")
    entry = await service.catalog.register(req.workflow)
    if entry.workflow.expose.a2a:
        from ..app import mount_agent  # late import: app imports this module

        mount_agent(request.app, service, entry.workflow)
    return summarize(entry, service.settings.public_url)


@router.delete("/v1/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, service: RunService = Depends(get_service)) -> Response:
    if not await service.catalog.delete(workflow_id):
        raise HTTPException(404, f"workflow '{workflow_id}' not found")
    return Response(status_code=204)
