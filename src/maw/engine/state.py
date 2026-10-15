"""Workflow state: the LangGraph state schema, its reducers, and the input for a new turn.

Every key has a reducer, so parallel branches can write the same key in one superstep.
Resets use a string sentinel (never a custom object) so checkpoint serializers can handle it.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

RESET = "__reset__"

STATE_KEYS = ("input", "messages", "nodes", "output", "vars", "visits", "errors", "trace")


def last_value(a: Any, b: Any) -> Any:
    return b


def merge_dicts(a: dict | None, b: dict | None) -> dict:
    if isinstance(b, dict) and b.get(RESET):
        return {}
    return {**(a or {}), **(b or {})}


def sum_counts(a: dict | None, b: dict | None) -> dict:
    if isinstance(b, dict) and b.get(RESET):
        return {}
    out = dict(a or {})
    for key, value in (b or {}).items():
        out[key] = out.get(key, 0) + value
    return out


def append_list(a: list | None, b: list | None) -> list:
    if b == [RESET]:
        return []
    return list(a or []) + list(b or [])


class WorkflowState(TypedDict, total=False):
    input: Annotated[Any, last_value]
    messages: Annotated[list[AnyMessage], add_messages]
    nodes: Annotated[dict[str, Any], merge_dicts]  # per-node results, keyed by node id
    output: Annotated[Any, last_value]  # latest result of a node with write_output=true
    vars: Annotated[dict[str, Any], merge_dicts]  # scratch space, e.g. a2a_contexts
    visits: Annotated[dict[str, int], sum_counts]  # loop guard
    errors: Annotated[list[dict], append_list]  # from on_error=continue nodes
    trace: Annotated[list[dict], append_list]  # per-node execution records


def new_turn_input(value: Any, messages: list[BaseMessage] | None = None) -> dict[str, Any]:
    """Input for a new run on a thread: ``messages`` and ``vars`` carry over, the rest resets."""
    if messages is None:
        from .paths import to_text

        messages = [HumanMessage(to_text(value))]
    return {
        "input": value,
        "messages": messages,
        "nodes": {RESET: True},
        "visits": {RESET: True},
        "errors": [RESET],
        "trace": [RESET],
        "output": None,
    }
