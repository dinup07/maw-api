"""MCP connector: tool discovery (cached) for LLM nodes and deterministic tool calls for ``mcp`` nodes."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import time
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from ..observability import metrics
from ..observability.tracing import span
from ..schema.workflow import McpServerSpec
from .registry import Resolved, resolve_secrets, resolve_secrets_deep

log = logging.getLogger("maw.mcp")


class McpToolError(RuntimeError):
    """An MCP tool reported an error, or the server could not be reached."""


def describe_error(exc: BaseException) -> str:
    """A readable message, digging through the ExceptionGroups that anyio task groups raise."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


def filter_tools(tools: list[BaseTool], allow: list[str], block: list[str]) -> list[BaseTool]:
    """Empty ``allow`` means all; ``block`` wins. Patterns are exact names or fnmatch globs."""
    out = []
    for tool in tools:
        if allow and not any(fnmatch.fnmatchcase(tool.name, p) for p in allow):
            continue
        if any(fnmatch.fnmatchcase(tool.name, p) for p in block):
            continue
        out.append(tool)
    return out


def parse_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured if isinstance(structured, dict) else {"result": structured}
    text = "".join(getattr(block, "text", "") for block in (result.content or []))
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}
    return value if isinstance(value, dict) else {"result": value}


class McpConnector:
    def __init__(self, *, tool_cache_ttl_s: float = 300, default_timeout_s: float = 60):
        self.tool_cache_ttl_s = tool_cache_ttl_s
        self.default_timeout_s = default_timeout_s
        self._tools: dict[str, tuple[float, list[BaseTool]]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _connection(self, spec: McpServerSpec, trusted: bool) -> dict[str, Any]:
        timeout = spec.timeout_s or self.default_timeout_s
        if spec.transport == "stdio":
            return {
                "transport": "stdio",
                "command": resolve_secrets(spec.command or "", trusted=trusted),
                "args": resolve_secrets_deep(spec.args, trusted=trusted),
                "env": resolve_secrets_deep(spec.env, trusted=trusted) or None,
            }
        conn: dict[str, Any] = {
            "transport": spec.transport,
            "url": resolve_secrets(spec.url or "", trusted=trusted),
            "headers": resolve_secrets_deep(spec.headers, trusted=trusted) or None,
        }
        conn["timeout"] = (
            timedelta(seconds=timeout) if spec.transport == "streamable_http" else float(timeout)
        )
        return conn

    async def list_tools(self, server: Resolved[McpServerSpec]) -> list[BaseTool]:
        conn = self._connection(server.spec, server.trusted)
        # Secrets are part of the connection, so they only enter the cache key as a hash.
        key = f"{server.name}:{hashlib.sha256(json.dumps(conn, sort_keys=True, default=str).encode()).hexdigest()}"
        cached = self._fresh(key)
        if cached is not None:
            return cached
        async with self._locks[key]:
            cached = self._fresh(key)
            if cached is not None:
                return cached
            client = MultiServerMCPClient({server.name: conn})
            with span("mcp.list_tools", server=server.name):
                try:
                    tools = await client.get_tools(server_name=server.name)
                except Exception as e:
                    raise McpToolError(f"MCP server '{server.name}' unreachable: {describe_error(e)}") from e
            log.info(
                "discovered MCP tools",
                extra={"data": {"server": server.name, "tools": [t.name for t in tools]}},
            )
            if self.tool_cache_ttl_s > 0:
                self._tools[key] = (time.monotonic(), tools)
            return tools

    def _fresh(self, key: str) -> list[BaseTool] | None:
        entry = self._tools.get(key)
        if entry is None or self.tool_cache_ttl_s <= 0:
            return None
        loaded_at, tools = entry
        if time.monotonic() - loaded_at > self.tool_cache_ttl_s:
            return None
        return tools

    async def tools_for(
        self, servers: list[tuple[Resolved[McpServerSpec], list[str], list[str]]]
    ) -> list[BaseTool]:
        discovered = await asyncio.gather(*(self.list_tools(server) for server, _, _ in servers))
        per_server = [
            (server.name, filter_tools(tools, allow, block))
            for (server, allow, block), tools in zip(servers, discovered, strict=True)
        ]
        counts = Counter(tool.name for _, tools in per_server for tool in tools)
        out: list[BaseTool] = []
        for name, tools in per_server:
            for tool in tools:
                if counts[tool.name] > 1:
                    tool = tool.model_copy(update={"name": f"{name}__{tool.name}"})
                out.append(tool)
        return out

    async def call_tool(
        self, server: Resolved[McpServerSpec], tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        client = MultiServerMCPClient({server.name: self._connection(server.spec, server.trusted)})
        started = time.perf_counter()
        outcome = "error"
        try:
            with span("mcp.call_tool", server=server.name, tool=tool):
                try:
                    async with client.session(server.name) as session:
                        result = await session.call_tool(tool, args)
                except Exception as e:
                    raise McpToolError(f"MCP server '{server.name}' call failed: {describe_error(e)}") from e
                if result.isError:
                    text = "".join(getattr(b, "text", "") for b in (result.content or [])) or "unknown error"
                    raise McpToolError(f"MCP tool '{tool}' on '{server.name}' failed: {text}")
            outcome = "ok"
            return parse_result(result)
        finally:
            metrics.mcp_calls_total.labels(server.name, outcome).inc()
            metrics.mcp_call_duration_seconds.labels(server.name).observe(time.perf_counter() - started)
