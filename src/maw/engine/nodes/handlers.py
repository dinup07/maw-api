"""Handlers for the ``mcp``, ``a2a``, ``function``, ``transform`` and ``human`` node types."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

from ...functions import get_function
from ...schema.workflow import A2aNode, FunctionNode, HumanNode, McpNode, TransformNode, WorkflowDefinition
from ..paths import input_text, render, resolve_args, resolve_inputs, resolve_value, to_text
from .base import EngineContext, NodeOutcome


async def run_mcp(node: McpNode, state: dict, wf: WorkflowDefinition, ctx: EngineContext) -> NodeOutcome:
    server = ctx.connections.mcp(node.server, wf)
    args = resolve_args(node.arguments, state)
    result = await ctx.mcp.call_tool(server, node.tool, args)
    return NodeOutcome(result=result, meta={"server": node.server, "tool": node.tool})


async def run_a2a(node: A2aNode, state: dict, wf: WorkflowDefinition, ctx: EngineContext) -> NodeOutcome:
    agent = ctx.connections.agent(node.agent, wf)
    names = resolve_inputs(node.inputs, state)
    text = render(node.message, state, names) if node.message is not None else input_text(state)
    data = resolve_value(node.data, state, names) if node.data is not None else None
    contexts = dict((state.get("vars") or {}).get("a2a_contexts") or {})
    context_id = contexts.get(node.agent) if node.keep_context else None

    res = await ctx.a2a.send(agent, text, data=data, context_id=context_id, streaming=node.streaming)
    result = res.as_dict()

    out_vars: dict[str, Any] = {}
    if node.keep_context and res.context_id:
        out_vars = {"a2a_contexts": {**contexts, node.agent: res.context_id}}
    messages = (
        [AIMessage(content=result["text"], name=node.id)]
        if node.append_to_messages and result["text"]
        else []
    )
    return NodeOutcome(
        result=result, messages=messages, vars=out_vars, meta={"agent": node.agent, "remote_state": res.state}
    )


async def run_function(
    node: FunctionNode, state: dict, wf: WorkflowDefinition, ctx: EngineContext
) -> NodeOutcome:
    fn = get_function(node.function)
    result = await fn.call(**resolve_args(node.arguments, state))
    return NodeOutcome(result=result, meta={"function": node.function})


async def run_transform(
    node: TransformNode, state: dict, wf: WorkflowDefinition, ctx: EngineContext
) -> NodeOutcome:
    return NodeOutcome(result=resolve_args(node.mapping, state))


async def run_human(node: HumanNode, state: dict, wf: WorkflowDefinition, ctx: EngineContext) -> NodeOutcome:
    # On resume the node re-executes from the top and interrupt() returns the supplied value.
    prompt = render(node.prompt, state, resolve_inputs(node.inputs, state))
    value = interrupt({"node": node.id, "prompt": prompt})
    result = value if isinstance(value, dict) else {"response": value}
    return NodeOutcome(result=result, messages=[HumanMessage(to_text(value))])
