"""Server-side connections: public view, MCP tool discovery and A2A agent cards."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from google.protobuf.json_format import MessageToDict

from ...runtime import RunService
from ..deps import get_service

router = APIRouter(prefix="/v1/connections", tags=["connections"])


@router.get("")
async def list_connections(service: RunService = Depends(get_service)) -> dict[str, Any]:
    return service.ctx.connections.describe()


@router.get("/mcp/{name}/tools")
async def mcp_tools(name: str, service: RunService = Depends(get_service)) -> list[dict[str, Any]]:
    connections = service.ctx.connections
    if name not in connections.mcp_servers:
        raise HTTPException(404, f"unknown MCP server '{name}'")
    tools = await service.ctx.mcp.list_tools(connections.mcp(name))
    out = []
    for tool in tools:
        schema = tool.args_schema
        if schema is not None and not isinstance(schema, dict):
            schema = schema.model_json_schema()
        out.append({"name": tool.name, "description": tool.description, "input_schema": schema})
    return out


@router.get("/agents/{name}/card")
async def agent_card(name: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    connections = service.ctx.connections
    if name not in connections.agents:
        raise HTTPException(404, f"unknown agent '{name}'")
    card = await service.ctx.a2a.card(connections.agent(name))
    return MessageToDict(card)
