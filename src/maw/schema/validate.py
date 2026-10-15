"""Parsing and graph-level validation of workflow definitions."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from . import compat
from .workflow import END, START, A2aNode, FunctionNode, LlmNode, McpNode, WorkflowDefinition


class WorkflowValidationError(Exception):
    """A workflow failed validation. ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def parse_workflow(raw: Any) -> tuple[WorkflowDefinition, list[str]]:
    """Normalize (reference format → native) and parse a definition into the Pydantic model."""
    try:
        native, warnings = compat.normalize(raw)
    except (KeyError, ValueError, TypeError) as e:
        detail = f"missing field {e}" if isinstance(e, KeyError) else str(e)
        raise WorkflowValidationError([f"reference format: {detail}"]) from e
    try:
        wf = WorkflowDefinition.model_validate(native)
    except ValidationError as e:
        raise WorkflowValidationError([_format_error(err) for err in e.errors()]) from e
    return wf, warnings


def _format_error(err: Any) -> str:
    loc = ".".join(str(p) for p in err["loc"])
    return f"{loc}: {err['msg']}" if loc else err["msg"]


def validate_graph(
    wf: WorkflowDefinition,
    *,
    known_mcp_servers: Iterable[str] = (),
    known_agents: Iterable[str] = (),
    known_functions: Iterable[str] = (),
) -> None:
    """Check the graph structure and references. Raises with **all** errors at once."""
    errors: list[str] = []
    mcp_servers = set(wf.mcp_servers) | set(known_mcp_servers)
    agents = set(wf.agents) | set(known_agents)
    functions = set(known_functions)

    # 1. Duplicate node ids.
    ids = [n.id for n in wf.nodes]
    seen: set[str] = set()
    for node_id in ids:
        if node_id in seen:
            errors.append(f"duplicate node id '{node_id}'")
        seen.add(node_id)
    node_ids = set(ids)

    # 2-3. Edge endpoints.
    by_source: dict[str, list] = defaultdict(list)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for i, edge in enumerate(wf.edges):
        src = edge.source
        where = f"edge {i} (source '{src}')"
        if src == END:
            errors.append(f"{where}: 'end' cannot be an edge source")
        elif src != START and src not in node_ids:
            errors.append(f"{where}: unknown node '{src}'")
        for target in edge.all_targets():
            if target == START:
                errors.append(f"{where}: 'start' cannot be an edge target")
            elif target != END and target not in node_ids:
                errors.append(f"{where}: target is unknown node '{target}'")
            else:
                adjacency[src].add(target)
        by_source[src].append(edge)

    # 4. A conditional edge must be the only edge from its source.
    for src, edges in by_source.items():
        if len(edges) > 1 and any(e.condition for e in edges):
            errors.append(
                f"node '{src}' has a conditional edge and {len(edges) - 1} other edge(s); "
                "merge them into one edge (put the default in 'target')"
            )

    # 5-8. Reachability.
    if START not in by_source:
        errors.append("there is no edge from 'start'")
    reachable: set[str] = set()
    queue = deque([START])
    while queue:
        for nxt in adjacency.get(queue.popleft(), ()):
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    unreachable = [n for n in ids if n not in reachable]
    if unreachable:
        errors.append(f"nodes unreachable from 'start': {unreachable}")
    if END not in reachable:
        errors.append("'end' is unreachable from 'start'")
    for node_id in ids:
        if node_id in reachable and node_id not in by_source:
            errors.append(f"node '{node_id}' has no outgoing edge (add an edge to 'end')")

    # 9-11. Per-node references and output schemas.
    for node in wf.nodes:
        label = f"node '{node.id}'"
        if isinstance(node, LlmNode):
            for ref in node.tools.mcp_refs():
                if ref.server not in mcp_servers:
                    errors.append(f"{label}: unknown MCP server '{ref.server}'")
            for name in node.tools.agents:
                if name not in agents:
                    errors.append(f"{label}: unknown agent '{name}'")
            if functions:
                for name in node.tools.functions:
                    if name not in functions:
                        errors.append(f"{label}: unknown function '{name}'")
            schema = node.output_schema
            if schema is not None and schema.get("type", "object") != "object":
                errors.append(f"{label}: output_schema must describe an object (type 'object')")
        elif isinstance(node, McpNode) and node.server not in mcp_servers:
            errors.append(f"{label}: unknown MCP server '{node.server}'")
        elif isinstance(node, A2aNode) and node.agent not in agents:
            errors.append(f"{label}: unknown agent '{node.agent}'")
        elif isinstance(node, FunctionNode) and functions and node.function not in functions:
            errors.append(f"{label}: unknown function '{node.function}'")

    if errors:
        raise WorkflowValidationError(errors)
