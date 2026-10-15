"""Built-in functions: calculator, json.parse, text.join, text.stats, dict.merge."""

from __future__ import annotations

import ast
import json
import operator
from typing import Any

from . import register_function

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MAX_EXPONENT = 100
_MAX_LENGTH = 500


def _evaluate(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"unsupported constant {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ValueError(f"exponent too large (max {_MAX_EXPONENT})")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_evaluate(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


@register_function("calculator")
def calculator(expression: str | None = None, input: str | None = None) -> dict[str, Any]:
    """Evaluate an arithmetic expression (numbers, + - * / // % ** and parentheses)."""
    expr = expression if expression is not None else input
    if not expr or not isinstance(expr, str):
        raise ValueError("calculator needs an 'expression'")
    if len(expr) > _MAX_LENGTH:
        raise ValueError(f"expression too long (max {_MAX_LENGTH} characters)")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"invalid expression: {e.msg}") from None
    result = _evaluate(tree)
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return {"expression": expr, "result": result}


@register_function("json.parse")
def json_parse(text: str) -> Any:
    """Parse a JSON string."""
    return json.loads(text)


@register_function("text.join")
def text_join(items: list, separator: str = "\n") -> dict[str, Any]:
    """Join items into one string."""
    return {"text": separator.join(str(i) for i in items)}


@register_function("text.stats")
def text_stats(text: str) -> dict[str, Any]:
    """Count characters, words and lines in a text."""
    return {"characters": len(text), "words": len(text.split()), "lines": max(1, len(text.splitlines()))}


@register_function("dict.merge")
def dict_merge(objects: list[dict]) -> dict[str, Any]:
    """Shallow-merge a list of objects; later ones win."""
    merged: dict[str, Any] = {}
    for obj in objects:
        merged.update(obj)
    return merged
