"""Translate reference-MAW workflow definitions into the native format.

``normalize`` is idempotent: a native definition is returned unchanged (as a deep copy), and the
output of a translation is itself native.
"""

from __future__ import annotations

import copy
import re
from typing import Any

_NODE_TYPES = {
    "llm_node": "llm",
    "enrichment_node": "transform",
    "a2a_node": "a2a",
    "mcp_node": "mcp",
    "human_node": "human",
}
# Keys that the reference format accepted but MAW has no equivalent for.
_DROPPED_KEYS = ("smax_params", "structured_output_mode", "model_provider")
# Run-request fields that may sit next to ``workflow_config``; RunRequest handles them.
_REQUEST_KEYS = {"input_prompt", "session_id", "async", "user_id"}

# A ``{key}`` placeholder that is not part of a ``{{ ... }}`` expression.
_SINGLE_BRACE = re.compile(r"(?<!\{)\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}(?!\})")


def is_reference(raw: dict[str, Any]) -> bool:
    if "workflow_config" in raw or "version_id" in raw or "config" in raw:
        return True
    if isinstance(raw.get("models"), list):
        return True
    nodes = raw.get("nodes")
    if isinstance(nodes, list):
        return any(isinstance(n, dict) and str(n.get("type", "")).endswith("_node") for n in nodes)
    return False


def normalize(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Return ``(native_definition, warnings)``.

    Raises ``KeyError``/``ValueError``/``TypeError`` for malformed reference definitions; the
    caller converts those into validation errors.
    """
    if not isinstance(raw, dict):
        raise TypeError("a workflow definition must be a JSON object")
    if not is_reference(raw):
        return copy.deepcopy(raw), []

    warnings: list[str] = []
    if "workflow_config" in raw:
        wf = copy.deepcopy(raw["workflow_config"])
        if not isinstance(wf, dict):
            raise TypeError("'workflow_config' must be a JSON object")
        config = copy.deepcopy(raw.get("config") or {})
        for key in raw:
            if key in ("workflow_config", "config") or key in _REQUEST_KEYS:
                continue
            if key == "id":
                wf.setdefault("id", raw["id"])
            else:
                warnings.append(f"ignored top-level field '{key}' outside 'workflow_config'")
    else:
        wf = copy.deepcopy(raw)
        config = wf.pop("config", None) or {}

    _apply_config(wf, config, warnings)

    if "version_id" in wf:
        version = wf.pop("version_id")
        if version is not None and "version" not in wf:
            wf["version"] = str(version)

    models = wf.get("models")
    if isinstance(models, list):
        converted: dict[str, Any] = {}
        for entry in models:
            entry = dict(entry)
            model_id = entry.pop("id")
            converted[model_id] = _model(model_id, entry, warnings)
        wf["models"] = converted
    elif isinstance(models, dict):
        wf["models"] = {k: _model(k, dict(v), warnings) for k, v in models.items()}

    if isinstance(wf.get("nodes"), list):
        wf["nodes"] = [_node(n, warnings) if isinstance(n, dict) else n for n in wf["nodes"]]
    return wf, warnings


def _apply_config(wf: dict[str, Any], config: dict[str, Any], warnings: list[str]) -> None:
    if not isinstance(config, dict):
        raise TypeError("'config' must be a JSON object")
    limits = {}
    for key, value in config.items():
        if key == "recursion_limit":
            limits["recursion_limit"] = value
        elif key == "max_iterations":
            limits["max_node_visits"] = value
        else:
            warnings.append(f"config.{key} is not supported and was dropped")
    if limits:
        wf.setdefault("limits", {}).update(limits)


def _model(model_id: str, spec: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if spec.get("provider") == "openai-compatible":
        spec["provider"] = "openai"
    params = dict(spec.get("params") or {})
    if "extra_params" in spec:
        params.update(spec.pop("extra_params") or {})
    if "chat_template_kwargs" in spec:
        extra_body = dict(params.get("extra_body") or {})
        extra_body["chat_template_kwargs"] = spec.pop("chat_template_kwargs")
        params["extra_body"] = extra_body
    if params:
        spec["params"] = params
    if "api_key_env_var" in spec:
        var = spec.pop("api_key_env_var")
        spec["api_key"] = f"${{secret:{var}}}"
        warnings.append(f"model '{model_id}': api_key now reads MAW_SECRET_{var}; set that env var")
    _drop(spec, f"model '{model_id}'", warnings)
    return spec


def _node(node: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    node = dict(node)
    node_id = node.get("id", "?")
    ref_type = node.get("type")
    label = f"node '{node_id}'"

    if "model_id" in node:
        node["model"] = node.pop("model_id")
    if "structured_output_schema" in node:
        node["output_schema"] = node.pop("structured_output_schema")
    if "skip_output_channel" in node:
        node["write_output"] = not node.pop("skip_output_channel")
    _drop(node, label, warnings)

    if ref_type == "tool_node":
        node["type"] = "function"
        node["function"] = node.pop("tool_name")
        node["arguments"] = node.pop("input_mapping", None) or {"input": "input"}
        return node
    if ref_type == "function_node":
        servers = node.pop("mcp_servers", None) or []
        if not servers:
            raise ValueError(f"{label}: function_node needs at least one entry in 'mcp_servers'")
        node["type"] = "mcp"
        node["server"] = servers[0]
        node["tool"] = node.pop("function_name")
        node["arguments"] = node.pop("input_mapping", None) or {}
        return node

    native = _NODE_TYPES.get(ref_type, ref_type)
    node["type"] = native
    if "input_mapping" in node:
        mapping = node.pop("input_mapping")
        if native == "mcp":
            node.setdefault("arguments", mapping)
        elif native == "transform":
            node.setdefault("mapping", mapping)
        else:
            node["inputs"] = mapping
    if isinstance(node.get("user_message"), str):
        node["user_message"] = _SINGLE_BRACE.sub(r"{{ \1 }}", node["user_message"])
    if native == "llm":
        _llm_tools(node, label, warnings)
    return node


def _llm_tools(node: dict[str, Any], label: str, warnings: list[str]) -> None:
    tools = node.get("tools")
    if isinstance(tools, list):
        tools = {"functions": list(tools)}
    servers = node.pop("mcp_servers", None)
    tool_filter = node.pop("tool_filter", None) or {}
    if servers:
        allow = _filter_entries(tool_filter.get("allowed") or [], label, warnings)
        block = _filter_entries(tool_filter.get("blocked") or [], label, warnings)
        if allow or block:
            refs: list[Any] = [{"server": s, "allow": allow, "block": block} for s in servers]
        else:
            refs = list(servers)
        tools = dict(tools or {})
        tools["mcp"] = list(tools.get("mcp") or []) + refs
    elif tool_filter:
        warnings.append(f"{label}: tool_filter without mcp_servers was dropped")
    if tools is not None:
        node["tools"] = tools


def _filter_entries(entries: list[str], label: str, warnings: list[str]) -> list[str]:
    out = []
    for entry in entries:
        if "__" in entry:
            out.append(entry)
        elif entry.startswith("tag:") or "." in entry:
            warnings.append(f"{label}: tag-based tool filter '{entry}' is not supported and was dropped")
        else:
            out.append(f"{entry}__*")
    return out


def _drop(spec: dict[str, Any], label: str, warnings: list[str]) -> None:
    for key in _DROPPED_KEYS:
        if key in spec:
            spec.pop(key)
            warnings.append(f"{label}: '{key}' is not supported and was dropped")
