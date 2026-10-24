"""Vector stores: list, ingest documents, query, delete documents.

Only stores from the server-side connections file are managed here; workflows may define their own
(qdrant) stores, but ingestion into them goes through their own tooling.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...engine.nodes.rag import retrieve
from ...runtime import RunService
from ..deps import get_service

router = APIRouter(prefix="/v1/vector-stores", tags=["vector stores"])


class DocumentIn(BaseModel):
    id: str | None = Field(None, max_length=256)  # defaults to a hash of the text; re-ingest replaces
    text: str = Field(min_length=1)
    metadata: dict[str, Any] = {}


class IngestRequest(BaseModel):
    documents: list[DocumentIn] = Field(min_length=1, max_length=1000)
    chunk_size: int = Field(1000, ge=50, le=20000)
    chunk_overlap: int = Field(150, ge=0, le=5000)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(4, ge=1, le=50)
    min_score: float | None = None
    filter: dict[str, Any] = {}


def _store(service: RunService, name: str):
    connections = service.ctx.connections
    if name not in connections.vector_stores:
        raise HTTPException(404, f"unknown vector store '{name}'")
    return connections.vector_store(name)


@router.get("")
async def list_vector_stores(service: RunService = Depends(get_service)) -> dict[str, Any]:
    return service.ctx.connections.describe()["vector_stores"]


@router.get("/{name}")
async def get_vector_store(name: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    store = _store(service, name)
    return {
        **service.ctx.connections.describe()["vector_stores"][name],
        "chunks": await service.ctx.vectors.count(store),
    }


@router.post("/{name}/documents")
async def ingest_documents(
    name: str, req: IngestRequest, service: RunService = Depends(get_service)
) -> dict[str, Any]:
    if req.chunk_overlap >= req.chunk_size:
        raise HTTPException(422, "chunk_overlap must be smaller than chunk_size")
    store = _store(service, name)
    return await service.ctx.vectors.add_documents(
        store,
        [d.model_dump() for d in req.documents],
        chunk_size=req.chunk_size,
        chunk_overlap=req.chunk_overlap,
    )


@router.post("/{name}/query")
async def query_vector_store(
    name: str, req: QueryRequest, service: RunService = Depends(get_service)
) -> dict[str, Any]:
    store = _store(service, name)
    return await retrieve(
        service.ctx, store, req.query, top_k=req.top_k, min_score=req.min_score, filter=req.filter
    )


@router.delete("/{name}/documents/{document_id:path}")
async def delete_document(
    name: str, document_id: str, service: RunService = Depends(get_service)
) -> dict[str, Any]:
    store = _store(service, name)
    deleted = await service.ctx.vectors.delete_document(store, document_id)
    if not deleted:
        raise HTTPException(404, f"document '{document_id}' not found in '{name}'")
    return {"document_id": document_id, "deleted_chunks": deleted}
