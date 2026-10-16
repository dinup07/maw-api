"""The ``rag`` node: retrieve passages from a vector store for later nodes (and the LLM retriever tool)."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ...connectors.registry import Resolved
from ...schema.workflow import RagNode, VectorStoreSpec, WorkflowDefinition
from ..paths import input_text, render, resolve_args, resolve_inputs
from .base import EngineContext, NodeOutcome

SOURCE_KEYS = ("source", "title", "url", "path")


def build_context(documents: list[dict[str, Any]], max_chars: int = 8000) -> str:
    """Numbered passages ready to drop into a prompt: ``[1] (source) text``."""
    parts: list[str] = []
    used = 0
    for i, doc in enumerate(documents, start=1):
        meta = doc.get("metadata") or {}
        source = next(
            (str(meta[k]) for k in SOURCE_KEYS if meta.get(k)), doc.get("document_id") or doc.get("id")
        )
        entry = f"[{i}] ({source}) {doc.get('text', '').strip()}"
        if used + len(entry) > max_chars:
            remaining = max_chars - used
            if remaining > 80:  # keep a useful partial passage rather than nothing
                parts.append(entry[:remaining].rstrip() + " …")
            break
        parts.append(entry)
        used += len(entry) + 2
    return "\n\n".join(parts)


async def retrieve(
    ctx: EngineContext,
    store: Resolved[VectorStoreSpec],
    query: str,
    *,
    top_k: int = 4,
    min_score: float | None = None,
    filter: dict[str, Any] | None = None,
    max_context_chars: int = 8000,
) -> dict[str, Any]:
    documents = await ctx.vectors.search(store, query, top_k=top_k, min_score=min_score, filter=filter)
    return {
        "query": query,
        "documents": documents,
        "count": len(documents),
        "context": build_context(documents, max_context_chars),
    }


async def run_rag(node: RagNode, state: dict, wf: WorkflowDefinition, ctx: EngineContext) -> NodeOutcome:
    store = ctx.connections.vector_store(node.store, wf)
    names = resolve_inputs(node.inputs, state)
    query = (render(node.query, state, names) if node.query is not None else input_text(state)).strip()
    meta = {"store": node.store}
    if not query:
        return NodeOutcome(
            result={"query": "", "documents": [], "count": 0, "context": ""}, meta={**meta, "hits": 0}
        )
    result = await retrieve(
        ctx,
        store,
        query,
        top_k=node.top_k,
        min_score=node.min_score,
        filter=resolve_args(node.filter, state),
        max_context_chars=node.max_context_chars,
    )
    return NodeOutcome(result=result, meta={**meta, "hits": result["count"]})


def retriever_tool(ctx: EngineContext, name: str, wf: WorkflowDefinition) -> BaseTool:
    """A tool the LLM can call to search a vector store; returns numbered passages."""
    store = ctx.connections.vector_store(name, wf)
    description = store.spec.description or f"Search the '{name}' knowledge base."

    async def search(query: str) -> str:
        result = await retrieve(ctx, store, query)
        return result["context"] or "No relevant passages found."

    return StructuredTool.from_function(
        coroutine=search,
        name="search_" + re.sub(r"[^A-Za-z0-9_-]", "_", name),
        description=f"{description} Pass a focused search query; returns numbered passages with sources.",
    )
