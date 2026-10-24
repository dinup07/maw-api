"""The single resolver for paths, value expressions and ``{{ }}`` templates.

Used everywhere a workflow reads state: ``inputs``, ``arguments``, ``mapping``, ``data``,
condition ``field``s and templates. Resolution never raises; anything missing is ``None``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .state import STATE_KEYS

_PLACEHOLDER = re.compile(r"\{\{\s*(.+?)\s*\}\}")
_MISSING = object()


def resolve_path(state: Mapping[str, Any], path: str) -> Any:
    parts = [p for p in path.strip().split(".") if p != ""]
    if parts and parts[0] == "state":
        parts = parts[1:]
    if not parts:
        return None
    if parts[0] not in STATE_KEYS:
        parts = ["nodes", *parts]
    return _walk(state, parts)


def _walk(value: Any, parts: list[str]) -> Any:
    for part in parts:
        value = _step(value, part)
        if value is None:
            return None
    return value


def _step(value: Any, part: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(part)
    if isinstance(value, (list, tuple)):
        try:
            return value[int(part)]
        except (ValueError, IndexError):
            return None
    if isinstance(value, str):
        return len(value) if part in ("length", "len") else None
    if part.startswith("_"):
        return None  # never reach into private or dunder attributes
    return getattr(value, part, None)


def _is_quoted(text: str) -> bool:
    return len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"')


def resolve_expr(expr: str, state: Mapping[str, Any], names: Mapping[str, Any] | None = None) -> Any:
    """Evaluate ``a.b || c.d || 'default'``: the first option that is not None wins."""
    for option in expr.split("||"):
        option = option.strip()
        if not option:
            continue
        if _is_quoted(option):
            return option[1:-1]
        value = _resolve_option(option, state, names)
        if value is not None:
            return value
    return None


def _resolve_option(option: str, state: Mapping[str, Any], names: Mapping[str, Any] | None) -> Any:
    if names:
        head, _, rest = option.partition(".")
        if head in names:
            value = names[head]
            return _walk(value, rest.split(".")) if rest else value
    return resolve_path(state, option)


def resolve_value(spec: Any, state: Mapping[str, Any], names: Mapping[str, Any] | None = None) -> Any:
    if isinstance(spec, dict):
        return {k: resolve_value(v, state, names) for k, v in spec.items()}
    if isinstance(spec, list):
        return [resolve_value(v, state, names) for v in spec]
    if not isinstance(spec, str):
        return spec
    if "{{" in spec:
        matches = list(_PLACEHOLDER.finditer(spec))
        if len(matches) == 1 and matches[0].span() == (0, len(spec)):
            return resolve_expr(matches[0].group(1), state, names)  # type preserved
        return render(spec, state, names)
    if _is_quoted(spec.strip()):
        return spec.strip()[1:-1]
    return resolve_expr(spec, state, names)


def render(template: str, state: Mapping[str, Any], names: Mapping[str, Any] | None = None) -> str:
    return _PLACEHOLDER.sub(lambda m: to_text(resolve_expr(m.group(1), state, names)), template)


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", _MISSING)
    if isinstance(content, str):
        return content
    return json.dumps(value, default=str, ensure_ascii=False)


def input_text(state: Mapping[str, Any]) -> str:
    return to_text(state.get("input"))


def resolve_inputs(inputs: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {k: resolve_value(v, state) for k, v in inputs.items()}


def resolve_args(arguments: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve an ``arguments``/``mapping`` dict, dropping keys whose value resolved to None."""
    resolved = {k: resolve_value(v, state) for k, v in arguments.items()}
    return {k: v for k, v in resolved.items() if v is not None}
