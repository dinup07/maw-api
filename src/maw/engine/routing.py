"""Edge conditions and routers."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from langgraph.graph import END as LG_END

from ..schema.workflow import END, Condition, Edge, Target
from .paths import resolve_value


def _graph_target(name: str) -> str:
    return LG_END if name == END else name


def static_targets(target: Target | None) -> list[str]:
    """Flatten a target into LangGraph node names (``end`` → LangGraph's END)."""
    if target is None:
        return []
    names = [target] if isinstance(target, str) else list(target)
    return [_graph_target(n) for n in names]


def _norm(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(cond: Condition, state: Mapping[str, Any]) -> bool:
    actual = resolve_value(cond.field, state)
    op, expected = cond.operator, cond.value

    if op == "exists":
        return actual is not None
    if op == "not_exists":
        return actual is None
    if op == "equals":
        return _norm(actual) == _norm(expected)
    if op == "not_equals":
        return _norm(actual) != _norm(expected)
    if op in ("in", "not_in"):
        options = expected if isinstance(expected, (list, tuple)) else [expected]
        found = _norm(actual) in [_norm(o) for o in options]
        return found if op == "in" else not found
    if op == "contains":
        if isinstance(actual, str):
            return str(expected).lower() in actual.lower()
        if isinstance(actual, (list, tuple)):
            return _norm(expected) in [_norm(a) for a in actual]
        if isinstance(actual, Mapping):
            return expected in actual
        return False
    if op == "matches":
        return actual is not None and re.search(str(expected), str(actual)) is not None
    a, b = _number(actual), _number(expected)
    if a is None or b is None:
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def make_router(edge: Edge) -> tuple[Callable[[Mapping[str, Any]], list[str]], list[str]]:
    """Build a router for a conditional edge. The first matching condition wins."""
    conditions = list(edge.condition or [])
    default = static_targets(edge.target) or [LG_END]

    def route(state: Mapping[str, Any]) -> list[str]:
        for cond in conditions:
            if evaluate(cond, state):
                return static_targets(cond.target)
        return default

    destinations = {_graph_target(t) for t in edge.all_targets()} | {LG_END}
    return route, sorted(destinations)
