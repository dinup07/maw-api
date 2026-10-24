"""Compile a validated workflow definition into a LangGraph ``StateGraph``, with an LRU cache.

No secrets are resolved at compile time; handlers resolve them per call.
"""

from __future__ import annotations

import time
from collections import OrderedDict, defaultdict
from typing import Any

from langgraph.graph import START as LG_START
from langgraph.graph import StateGraph

from ..observability import metrics
from ..schema.workflow import START, Edge, WorkflowDefinition
from .nodes import HANDLERS, EngineContext, wrap_node
from .routing import make_router, static_targets
from .state import WorkflowState


def compile_workflow(wf: WorkflowDefinition, ctx: EngineContext, checkpointer: Any = None) -> Any:
    """Build the graph. Assumes ``validate_graph`` has passed."""
    graph = StateGraph(WorkflowState)
    for node in wf.nodes:
        graph.add_node(node.id, wrap_node(node, HANDLERS[node.type], wf, ctx), defer=node.defer)

    by_source: dict[str, list[Edge]] = defaultdict(list)
    for edge in wf.edges:
        by_source[edge.source].append(edge)
    for source, edges in by_source.items():
        src = LG_START if source == START else source
        conditional = next((e for e in edges if e.condition), None)
        if conditional is not None:
            router, destinations = make_router(conditional)
            graph.add_conditional_edges(src, router, destinations)
            continue
        for edge in edges:
            for target in static_targets(edge.target):
                graph.add_edge(src, target)
    return graph.compile(checkpointer=checkpointer, name=wf.id)


class GraphCache:
    """LRU of compiled graphs keyed by the workflow's content hash."""

    def __init__(self, size: int = 128):
        self.size = size
        self._graphs: OrderedDict[str, Any] = OrderedDict()

    def get_or_compile(
        self, wf: WorkflowDefinition, ctx: EngineContext, checkpointer: Any = None
    ) -> tuple[Any, str]:
        key = wf.content_hash()
        graph = self._graphs.get(key)
        if graph is not None:
            self._graphs.move_to_end(key)
            metrics.graph_cache_total.labels("hit").inc()
            return graph, key
        metrics.graph_cache_total.labels("miss").inc()
        started = time.perf_counter()
        graph = compile_workflow(wf, ctx, checkpointer)
        metrics.graph_compile_seconds.observe(time.perf_counter() - started)
        self._graphs[key] = graph
        while len(self._graphs) > self.size:
            self._graphs.popitem(last=False)
        return graph, key

    def __len__(self) -> int:
        return len(self._graphs)

    def info(self) -> dict[str, int]:
        return {"size": len(self._graphs), "capacity": self.size}


__all__ = ["GraphCache", "compile_workflow"]
