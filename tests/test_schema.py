"""The workflow contract: parsing, graph validation, content hashing and reference-format compat."""

from __future__ import annotations

import json

import pytest
from conftest import WORKFLOWS_DIR, chain, wf

from maw.functions import function_names
from maw.schema import WorkflowValidationError, normalize, parse_workflow, validate_graph

KNOWN = {"known_mcp_servers": ["kb"], "known_agents": ["billing"]}


def check(raw: dict, **known) -> None:
    definition, _ = parse_workflow(raw)
    validate_graph(definition, **known)


def errors_of(raw: dict, **known) -> str:
    with pytest.raises(WorkflowValidationError) as exc:
        check(raw, **known)
    return "\n".join(exc.value.errors)


LLM = {"type": "llm", "model": "scripted"}


# 1 ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted(WORKFLOWS_DIR.glob("*.json")), ids=lambda p: p.name)
def test_catalog_workflows_validate(path):
    definition, warnings = parse_workflow(json.loads(path.read_text()))
    validate_graph(definition, known_functions=function_names(), **KNOWN)
    assert definition.id == path.stem
    assert warnings == []


def test_catalog_has_five_workflows():
    assert len(list(WORKFLOWS_DIR.glob("*.json"))) == 5


# 2 ---------------------------------------------------------------------------------------------------
def test_unknown_field_rejected():
    raw = wf([{"id": "a", **LLM, "sytem_prompt": "hi"}], chain("a"))
    assert "sytem_prompt" in errors_of(raw)


# 3 ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad_id", ["end", "start", "bad id!"])
def test_reserved_and_invalid_ids_rejected(bad_id):
    raw = wf([{"id": bad_id, **LLM}], [{"source": "start", "target": "end"}])
    assert "nodes.0" in errors_of(raw)


def test_invalid_workflow_id_rejected():
    assert "id" in errors_of(wf([{"id": "a", **LLM}], chain("a"), id="has space"))


# 4 ---------------------------------------------------------------------------------------------------
def test_graph_errors_reported_together():
    raw = wf(
        [{"id": n, **LLM} for n in ("a", "orphan", "dead_end")],
        [
            {"source": "start", "target": ["a", "dead_end"]},
            {"source": "a", "target": "ghost"},
            {"source": "orphan", "target": "end"},
        ],
    )
    text = errors_of(raw)
    assert "unknown node 'ghost'" in text
    assert "nodes unreachable from 'start': ['orphan']" in text
    assert "node 'dead_end' has no outgoing edge (add an edge to 'end')" in text
    assert "'end' is unreachable" in text


def test_missing_start_edge_and_end_as_source():
    raw = wf([{"id": "a", **LLM}], [{"source": "end", "target": "a"}, {"source": "a", "target": "end"}])
    text = errors_of(raw)
    assert "no edge from 'start'" in text
    assert "'end' cannot be an edge source" in text


def test_duplicate_node_ids():
    raw = wf([{"id": "a", **LLM}, {"id": "a", **LLM}], chain("a"))
    assert "duplicate node id 'a'" in errors_of(raw)


# 5 ---------------------------------------------------------------------------------------------------
def test_conditional_and_plain_edge_from_one_source():
    raw = wf(
        [{"id": n, **LLM} for n in ("a", "b", "c")],
        [
            {"source": "start", "target": "a"},
            {"source": "a", "condition": [{"field": "output.x", "value": 1, "target": "b"}]},
            {"source": "a", "target": "c"},
            {"source": "b", "target": "end"},
            {"source": "c", "target": "end"},
        ],
    )
    assert "merge them into one edge" in errors_of(raw)


def test_edge_needs_target_or_condition():
    raw = wf([{"id": "a", **LLM}], [*chain("a"), {"source": "a"}])
    assert "needs 'target' or a non-empty 'condition'" in errors_of(raw)


# 6 ---------------------------------------------------------------------------------------------------
def test_unknown_references_reported():
    raw = wf(
        [
            {"id": "m", "type": "mcp", "server": "nope", "tool": "t"},
            {"id": "x", "type": "a2a", "agent": "ghost"},
            {"id": "f", "type": "function", "function": "x.y"},
            {
                "id": "l",
                **LLM,
                "tools": {"mcp": [{"server": "nada"}], "agents": ["phantom"], "functions": ["z.z"]},
            },
        ],
        chain("m", "x", "f", "l"),
    )
    text = errors_of(raw, known_functions=function_names())
    assert "unknown MCP server 'nope'" in text
    assert "unknown agent 'ghost'" in text
    assert "unknown function 'x.y'" in text
    assert "unknown MCP server 'nada'" in text
    assert "unknown agent 'phantom'" in text
    assert "unknown function 'z.z'" in text


def test_functions_not_checked_without_known_functions():
    check(wf([{"id": "f", "type": "function", "function": "anything"}], chain("f")))


def test_known_connections_satisfy_references():
    raw = wf(
        [
            {"id": "m", "type": "mcp", "server": "kb", "tool": "search_kb"},
            {"id": "b", "type": "a2a", "agent": "billing"},
        ],
        chain("m", "b"),
    )
    check(raw, **KNOWN)


def test_output_schema_must_be_object():
    raw = wf([{"id": "a", **LLM, "output_schema": {"type": "array"}}], chain("a"))
    assert "output_schema must describe an object" in errors_of(raw)
    check(wf([{"id": "a", **LLM, "output_schema": {"properties": {"x": {"type": "string"}}}}], chain("a")))


# 7 ---------------------------------------------------------------------------------------------------
def test_inline_connections_satisfy_references():
    raw = wf(
        [
            {"id": "m", "type": "mcp", "server": "mine", "tool": "t"},
            {"id": "b", "type": "a2a", "agent": "peer"},
            {"id": "l", **LLM, "tools": {"mcp": ["mine"], "agents": ["peer"]}},
        ],
        chain("m", "b", "l"),
        mcp_servers={"mine": {"url": "http://localhost:1/mcp"}},
        agents={"peer": {"url": "http://localhost:2"}},
    )
    check(raw)


# 8 ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({"transport": "stdio"}, "stdio MCP servers need 'command'"),
        ({"transport": "sse"}, "sse MCP servers need 'url'"),
    ],
)
def test_mcp_server_transport_requirements(server, message):
    raw = wf([{"id": "a", **LLM}], chain("a"), mcp_servers={"s": server})
    assert message in errors_of(raw)


# 9 ---------------------------------------------------------------------------------------------------
def test_content_hash_stable_and_sensitive():
    raw = wf([{"id": "a", **LLM, "system_prompt": "one"}], chain("a"))
    first, _ = parse_workflow(raw)
    again, _ = parse_workflow(json.loads(json.dumps(first.model_dump(mode="json"))))
    assert first.content_hash() == again.content_hash()
    raw["nodes"][0]["system_prompt"] = "two"
    changed, _ = parse_workflow(raw)
    assert changed.content_hash() != first.content_hash()


# 10 --------------------------------------------------------------------------------------------------
REFERENCE = {
    "workflow_config": {
        "id": "legacy_flow",
        "version_id": 3,
        "models": [
            {
                "id": "local",
                "provider": "openai-compatible",
                "model": "qwen3",
                "base_url": "http://llm.internal/v1",
                "api_key_env_var": "LOCAL_KEY",
                "chat_template_kwargs": {"enable_thinking": False},
                "extra_params": {"top_p": 0.9},
                "smax_params": {"x": 1},
            }
        ],
        "nodes": [
            {
                "id": "analyze",
                "type": "llm_node",
                "model_id": "local",
                "user_message": "Customer {name} says: {{ input }} / {nodes.prev.x}",
                "input_mapping": {"name": "input.customer"},
                "structured_output_schema": {"type": "object", "properties": {"s": {"type": "string"}}},
                "mcp_servers": ["kb"],
                "tool_filter": {
                    "allowed": ["kb__search_kb", "tickets", "tag:read", "kb.readonly"],
                    "blocked": [],
                },
                "structured_output_mode": "json",
            },
            {
                "id": "lookup",
                "type": "function_node",
                "function_name": "get_ticket",
                "mcp_servers": ["kb"],
                "input_mapping": {"ticket_id": "input"},
            },  # fmt: skip
            {"id": "calc", "type": "tool_node", "tool_name": "calculator"},
            {
                "id": "enrich",
                "type": "enrichment_node",
                "mapping": {"text": "nodes.analyze.s"},
                "skip_output_channel": True,
            },  # fmt: skip
            {
                "id": "plain",
                "type": "llm_node",
                "model": "scripted",
                "mcp_servers": ["kb"],
                "tools": ["calculator"],
            },
        ],
        "edges": chain("analyze", "lookup", "calc", "enrich", "plain"),
    },
    "config": {"recursion_limit": 30, "max_iterations": 4},
    "input_prompt": "hello",
    "session_id": "s-1",
}


def test_reference_format_translates():
    native, warnings = normalize(REFERENCE)
    assert "workflow_config" not in native and "config" not in native
    assert native["version"] == "3"
    assert native["limits"] == {"recursion_limit": 30, "max_node_visits": 4}

    model = native["models"]["local"]
    assert model["provider"] == "openai"
    assert model["api_key"] == "${secret:LOCAL_KEY}"
    assert model["params"] == {
        "top_p": 0.9,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    assert "smax_params" not in model

    nodes = {n["id"]: n for n in native["nodes"]}
    analyze = nodes["analyze"]
    assert analyze["type"] == "llm" and analyze["model"] == "local"
    assert analyze["inputs"] == {"name": "input.customer"}
    assert analyze["output_schema"]["properties"] == {"s": {"type": "string"}}
    assert analyze["user_message"] == "Customer {{ name }} says: {{ input }} / {{ nodes.prev.x }}"
    assert analyze["tools"] == {
        "mcp": [{"server": "kb", "allow": ["kb__search_kb", "tickets__*"], "block": []}]
    }
    assert "structured_output_mode" not in analyze

    assert nodes["lookup"] == {"id": "lookup", "type": "mcp", "server": "kb", "tool": "get_ticket",
                               "arguments": {"ticket_id": "input"}}  # fmt: skip
    assert nodes["calc"] == {
        "id": "calc",
        "type": "function",
        "function": "calculator",
        "arguments": {"input": "input"},
    }
    assert nodes["enrich"]["type"] == "transform" and nodes["enrich"]["write_output"] is False
    assert nodes["plain"]["tools"] == {"functions": ["calculator"], "mcp": ["kb"]}

    joined = "\n".join(warnings)
    assert "MAW_SECRET_LOCAL_KEY" in joined
    assert "'tag:read'" in joined and "'kb.readonly'" in joined
    assert "smax_params" in joined and "structured_output_mode" in joined

    definition, _ = parse_workflow(REFERENCE)
    validate_graph(definition, known_functions=function_names(), **KNOWN)
    assert definition.id == "legacy_flow"
    assert normalize(native) == (native, [])  # translation output is native (idempotent)


def test_function_node_without_servers_is_an_error():
    raw = {
        "workflow_config": {
            "nodes": [{"id": "x", "type": "function_node", "function_name": "t"}],
            "edges": chain("x"),
        }
    }
    assert "function_node needs at least one entry" in errors_of(raw)


def test_reference_example_file_parses():
    raw = json.loads((WORKFLOWS_DIR.parent / "examples" / "reference_format.json").read_text())
    definition, _ = parse_workflow(raw)
    validate_graph(definition)
    assert definition.nodes[0].type == "llm"
    assert definition.nodes[0].output_schema["required"] == ["sentiment"]


# 11 --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted(WORKFLOWS_DIR.glob("*.json")), ids=lambda p: p.name)
def test_native_definition_passes_through_unchanged(path):
    raw = json.loads(path.read_text())
    native, warnings = normalize(raw)
    assert native == raw and native is not raw
    assert warnings == []


def test_json_schema_title():
    from maw.schema import WorkflowDefinition

    assert WorkflowDefinition.model_json_schema()["title"] == "WorkflowDefinition"
