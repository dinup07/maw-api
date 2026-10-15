"""Liveness, readiness and Prometheus metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ... import __version__
from ...runtime import RunService
from ..deps import get_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/ready")
async def ready(service: RunService = Depends(get_service)) -> JSONResponse:
    try:
        await service.store.list_runs(limit=1)  # proves the store is reachable
    except Exception as e:
        return JSONResponse(
            status_code=503, content={"status": "unavailable", "error": f"{type(e).__name__}: {e}"}
        )
    return JSONResponse(
        {
            "status": "ready",
            "durable": service.durable,
            "workflows": len(service.catalog),
            "graph_cache": service.cache.info(),
        }
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
