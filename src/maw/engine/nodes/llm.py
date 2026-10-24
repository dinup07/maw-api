"""The ``llm`` node: prompt assembly, an optional tool loop, and optional structured output."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import jsonschema
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphBubbleUp

from ...functions import get_function
from ...observability import metrics
from ...observability.tracing import span
from ...schema.workflow import LlmNode, WorkflowDefinition
from ..paths import input_text, render, resolve_inputs, to_text
from .base import EngineContext, NodeOutcome
from .rag import retriever_tool

FINAL_ANSWER_PROMPT = "Now give your final answer in the required structured format."
TOOL_LIMIT_MESSAGE = "Tool call limit reached. Answer with what you have."


class StructuredOutputError(ValueError):
    """The model's structured output did not match the node's ``output_schema``."""


def message_text(msg: BaseMessage) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


async def invoke(model: Any, messages: list[BaseMessage], label: str) -> Any:
    started = time.perf_counter()
    with span("llm.invoke", model=label):
        try:
            result = await model.ainvoke(messages)
        except Exception:
            metrics.llm_calls_total.labels(label, "error").inc()
            raise
        finally:
            metrics.llm_call_duration_seconds.labels(label).observe(time.perf_counter() - started)
    metrics.llm_calls_total.labels(label, "ok").inc()
    usage = getattr(result, "usage_metadata", None)
    if usage:
        metrics.llm_tokens_total.labels(label, "input").inc(usage.get("input_tokens", 0))
        metrics.llm_tokens_total.labels(label, "output").inc(usage.get("output_tokens", 0))
    return result


def _history(node: LlmNode, state: dict) -> list[BaseMessage]:
    if node.history == "none":
        return []
    messages = list(state.get("messages") or [])
    if isinstance(node.history, int):
        messages = messages[-node.history :] if node.history else []
    # Tool chatter from other nodes would be orphaned; providers reject it.
    return [
        m for m in messages if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)
    ]


async def _build_tools(node: LlmNode, wf: WorkflowDefinition, ctx: EngineContext) -> list[BaseTool]:
    tools: list[BaseTool] = []
    refs = node.tools.mcp_refs()
    if refs:
        servers = [(ctx.connections.mcp(ref.server, wf), ref.allow, ref.block) for ref in refs]
        tools.extend(await ctx.mcp.tools_for(servers))

    for name in node.tools.agents:
        agent = ctx.connections.agent(name, wf)
        description = agent.spec.description
        if not description:
            try:
                description = (await ctx.a2a.card(agent)).description
            except (GraphBubbleUp, asyncio.CancelledError):
                raise
            except Exception:
                description = None
        description = description or f"Ask the '{name}' agent."

        def make_ask(resolved: Any):
            async def ask_agent(message: str) -> str:
                res = await ctx.a2a.send(resolved, message)
                out = res.as_dict()
                return (
                    json.dumps({"text": out["text"], "data": out["data"]})
                    if out["data"] is not None
                    else out["text"]
                )

            return ask_agent

        tools.append(
            StructuredTool.from_function(
                coroutine=make_ask(agent),
                name=_sanitize(name),
                description=f"{description} Send it a complete, self-contained request in 'message'.",
            )
        )

    tools.extend(get_function(name).as_tool() for name in node.tools.functions)
    tools.extend(retriever_tool(ctx, name, wf) for name in node.tools.retrievers)
    return tools


async def _run_tool(tool: BaseTool | None, call: dict) -> ToolMessage:
    if tool is None:
        return ToolMessage(f"Error: unknown tool '{call['name']}'", tool_call_id=call["id"], status="error")
    with span("llm.tool_call", tool=call["name"]):
        try:
            result = await tool.ainvoke({**call, "type": "tool_call"})
        except (GraphBubbleUp, asyncio.CancelledError):
            raise
        except Exception as e:  # let the model see the error and recover
            return ToolMessage(f"Error: {type(e).__name__}: {e}", tool_call_id=call["id"], status="error")
    if isinstance(result, ToolMessage):
        return result
    return ToolMessage(to_text(result), tool_call_id=call["id"])


async def _structured(node: LlmNode, model: Any, messages: list[BaseMessage], label: str) -> dict:
    schema = dict(node.output_schema or {})
    schema.setdefault("type", "object")
    schema.setdefault("title", f"{_sanitize(node.id)}_output")
    schema.setdefault("description", f"Structured output of node {node.id}")
    raw = await invoke(model.with_structured_output(schema), messages, label)
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    try:
        if not isinstance(raw, dict):
            raise StructuredOutputError(
                f"output did not match output_schema: expected an object, got {type(raw).__name__}"
            )
        try:
            jsonschema.validate(raw, node.output_schema or {})
        except jsonschema.ValidationError as e:
            raise StructuredOutputError(f"output did not match output_schema: {e.message}") from None
    except StructuredOutputError:
        if node.recover_text_field:
            return {node.recover_text_field: raw if isinstance(raw, str) else json.dumps(raw)}
        raise
    return raw


async def run_llm(node: LlmNode, state: dict, wf: WorkflowDefinition, ctx: EngineContext) -> NodeOutcome:
    model, label = ctx.models.get(node.model, wf)
    names = resolve_inputs(node.inputs, state)

    messages: list[BaseMessage] = []
    if node.system_prompt:
        messages.append(SystemMessage(render(node.system_prompt, state, names)))
    history = _history(node, state)
    messages.extend(history)
    if node.user_message is not None:
        messages.append(HumanMessage(render(node.user_message, state, names)))
    elif not history:
        messages.append(HumanMessage(input_text(state)))

    made = 0
    if not node.tools.is_empty():
        tools = await _build_tools(node, wf, ctx)
        by_name = {t.name: t for t in tools}
        bound = model.bind_tools(tools)
        budget = node.max_tool_calls if node.max_tool_calls is not None else ctx.default_max_tool_calls
        while True:
            ai = await invoke(bound, messages, label)
            messages.append(ai)
            if not ai.tool_calls:
                break
            if made + len(ai.tool_calls) > budget:
                for call in ai.tool_calls:
                    messages.append(ToolMessage(TOOL_LIMIT_MESSAGE, tool_call_id=call["id"], status="error"))
                messages.append(await invoke(model, messages, label))
                break
            results = await asyncio.gather(*(_run_tool(by_name.get(c["name"]), c) for c in ai.tool_calls))
            made += len(ai.tool_calls)
            messages.extend(results)
        if node.output_schema is not None:
            messages.append(HumanMessage(FINAL_ANSWER_PROMPT))
            result = await _structured(node, model, messages, label)
        else:
            result = {"text": message_text(messages[-1])}
    elif node.output_schema is not None:
        result = await _structured(node, model, messages, label)
    else:
        result = {"text": message_text(await invoke(model, messages, label))}

    out_messages: list[BaseMessage] = []
    if node.append_to_messages:
        text = result.get("text") if isinstance(result.get("text"), str) else None
        out_messages.append(AIMessage(content=text or json.dumps(result), name=node.id))
    return NodeOutcome(result=result, messages=out_messages, meta={"model": label, "tool_calls": made})
