"""Registry of local Python functions callable from ``function`` nodes and as LLM tools."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

_REGISTRY: dict[str, RegisteredFunction] = {}


@dataclass(frozen=True)
class RegisteredFunction:
    name: str
    fn: Callable[..., Any]
    description: str

    async def call(self, **kwargs: Any) -> Any:
        result = self.fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def as_tool(self) -> BaseTool:
        name = self.name.replace(".", "_")
        if inspect.iscoroutinefunction(self.fn):
            return StructuredTool.from_function(coroutine=self.fn, name=name, description=self.description)
        return StructuredTool.from_function(func=self.fn, name=name, description=self.description)


def register_function(name: str, description: str | None = None) -> Callable[[Callable], Callable]:
    """Decorator: register ``fn`` under ``name``. The description defaults to the docstring."""

    def decorator(fn: Callable) -> Callable:
        desc = description or inspect.getdoc(fn) or f"Function '{name}'."
        _REGISTRY[name] = RegisteredFunction(name=name, fn=fn, description=desc)
        return fn

    return decorator


def get_function(name: str) -> RegisteredFunction:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown function '{name}'") from None


def function_names() -> list[str]:
    return sorted(_REGISTRY)


def load_function_modules(modules: list[str]) -> None:
    """Import the built-ins, then each extra module (importing registers its functions)."""
    importlib.import_module("maw.functions.builtin")
    for module in modules:
        importlib.import_module(module)
