"""Engine context, node outcomes and the wrapper that adds retries, timeouts, loop guards and tracing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.messages import BaseMessage
from langgraph.errors import GraphBubbleUp

from ...observability import metrics
from ...observability.logging import bind, node_id_var
from ...observability.tracing import span

if TYPE_CHECKING:
    from ...connectors.a2a_client import A2AConnector
    from ...connectors.mcp_client import McpConnector
    from ...connectors.registry import ConnectionRegistry
    from ...connectors.vectorstores import VectorStoreConnector
    from ...schema.workflow import WorkflowDefinition
    from ..models import ModelRegistry

log = logging.getLogger("maw.engine")


@dataclass
class EngineContext:
    """Shared, long-lived dependencies of node handlers."""

    connections: ConnectionRegistry
    models: ModelRegistry
    mcp: McpConnector | None = None
    a2a: A2AConnector | None = None
    vectors: VectorStoreConnector | None = None
    default_max_tool_calls: int = 8
    default_max_node_visits: int = 10


@dataclass
class NodeOutcome:
    result: Any
    messages: list[BaseMessage] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Any, dict, "WorkflowDefinition", EngineContext], Awaitable[NodeOutcome]]


class NodeLoopLimitError(RuntimeError):
    """A node was visited more often than its ``max_visits`` allows."""


class NodeExecutionError(RuntimeError):
    def __init__(self, node_id: str, cause: BaseException):
        self.node_id = node_id
        self.cause = cause
        super().__init__(f"node '{node_id}' failed: {type(cause).__name__}: {cause}")


def wrap_node(node: Any, handler: Handler, wf: WorkflowDefinition, ctx: EngineContext) -> Callable:
    max_visits = node.max_visits or wf.limits.max_node_visits or ctx.default_max_node_visits
    attempts = node.retry.max_attempts

    async def run(state: dict) -> dict:
        with bind(node_id_var, node.id), span(f"node.{node.id}", node_type=node.type, node_id=node.id):
            if (state.get("visits") or {}).get(node.id, 0) >= max_visits:
                raise NodeLoopLimitError(
                    f"node '{node.id}' reached max_visits={max_visits}; check for an unbounded loop"
                )
            started = time.perf_counter()
            outcome: NodeOutcome | None = None
            error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    call = handler(node, state, wf, ctx)
                    outcome = await (asyncio.wait_for(call, node.timeout_s) if node.timeout_s else call)
                    error = None
                    break
                except (GraphBubbleUp, asyncio.CancelledError):
                    raise  # interrupts and cancellation are control flow
                except TimeoutError:
                    error = TimeoutError(f"timed out after {node.timeout_s}s")
                except Exception as e:
                    error = e
                if attempt < attempts:
                    delay = node.retry.backoff_s * 2 ** (attempt - 1)
                    metrics.node_retries_total.labels(node.type).inc()
                    log.warning(
                        "node attempt failed; retrying",
                        extra={"data": {"attempt": attempt, "error": str(error), "delay_s": delay}},
                    )
                    await asyncio.sleep(delay)

            elapsed = time.perf_counter() - started
            ms = round(elapsed * 1000, 2)
            update: dict[str, Any] = {"visits": {node.id: 1}}

            if error is not None:
                metrics.node_duration_seconds.labels(node.type, "error").observe(elapsed)
                if node.on_error == "fail":
                    raise NodeExecutionError(node.id, error) from error
                result = {"error": str(error), "error_type": type(error).__name__}
                log.warning("node failed; continuing (on_error=continue)", extra={"data": result})
                update["nodes"] = {node.id: result}
                update["errors"] = [{"node": node.id, "type": type(error).__name__, "message": str(error)}]
                update["trace"] = [{"node": node.id, "type": node.type, "ms": ms, "status": "error"}]
                if node.write_output:
                    update["output"] = result
                return update

            metrics.node_duration_seconds.labels(node.type, "ok").observe(elapsed)
            assert outcome is not None
            result = outcome.result if isinstance(outcome.result, dict) else {"result": outcome.result}
            update["nodes"] = {node.id: result}
            update["trace"] = [{"node": node.id, "type": node.type, "ms": ms, "status": "ok", **outcome.meta}]
            if node.write_output:
                update["output"] = result
            if outcome.messages:
                update["messages"] = outcome.messages
            if outcome.vars:
                update["vars"] = outcome.vars
            return update

    run.__name__ = f"node_{node.id}"
    return run
