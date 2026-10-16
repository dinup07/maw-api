"""Node handlers. To add a node type: add a schema model and a ``HANDLERS`` entry."""

from .base import EngineContext, NodeExecutionError, NodeLoopLimitError, NodeOutcome, wrap_node
from .handlers import run_a2a, run_function, run_human, run_mcp, run_transform
from .llm import StructuredOutputError, run_llm
from .rag import run_rag

HANDLERS = {
    "llm": run_llm,
    "mcp": run_mcp,
    "a2a": run_a2a,
    "function": run_function,
    "transform": run_transform,
    "human": run_human,
    "rag": run_rag,
}

__all__ = [
    "HANDLERS",
    "EngineContext",
    "NodeExecutionError",
    "NodeLoopLimitError",
    "NodeOutcome",
    "StructuredOutputError",
    "wrap_node",
]
