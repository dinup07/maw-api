"""Agent cards and mounting a workflow's A2A routes under ``/a2a/<id>``."""

from __future__ import annotations

from typing import Any

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import TransportProtocol
from starlette.routing import Mount

from .. import __version__
from ..runtime import RunService
from ..schema import WorkflowDefinition
from .executor import WorkflowAgentExecutor

MODES = ["text/plain", "application/json"]


def agent_base_url(public_url: str, workflow_id: str) -> str:
    return f"{public_url.rstrip('/')}/a2a/{workflow_id}"


def build_agent_card(wf: WorkflowDefinition, public_url: str) -> AgentCard:
    base = agent_base_url(public_url, wf.id)
    name = wf.expose.name or wf.id
    description = wf.expose.description or wf.description or f"MAW workflow '{wf.id}'"
    if wf.expose.skills:
        skills = [
            AgentSkill(id=s.id, name=s.name, description=s.description, tags=s.tags, examples=s.examples)
            for s in wf.expose.skills
        ]
    else:
        skills = [AgentSkill(id=wf.id, name=name, description=description, tags=["workflow"])]
    return AgentCard(
        name=name,
        description=description,
        version=wf.version or __version__,
        supported_interfaces=[
            AgentInterface(url=base, protocol_binding=TransportProtocol.JSONRPC, protocol_version="1.0"),
            AgentInterface(url=base, protocol_binding=TransportProtocol.HTTP_JSON, protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=wf.expose.streaming),
        default_input_modes=MODES,
        default_output_modes=MODES,
        skills=skills,
    )


def mount_workflow_agent(app: Any, service: RunService, wf: WorkflowDefinition, public_url: str) -> AgentCard:
    prefix = f"/a2a/{wf.id}"
    card = build_agent_card(wf, public_url)

    async def current_card(default: AgentCard) -> AgentCard:
        # Re-registering a workflow updates its card without remounting.
        entry = service.catalog.get(wf.id)
        return build_agent_card(entry.workflow, public_url) if entry else default

    handler = DefaultRequestHandler(
        agent_executor=WorkflowAgentExecutor(service, wf), task_store=InMemoryTaskStore(), agent_card=card
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            card, card_modifier=current_card, card_url=f"{prefix}/.well-known/agent-card.json"
        ),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=prefix),
        # SDK bug (spec trap T7): create_rest_routes adds Mount('/{tenant}') at the app root, which
        # would swallow every agent mounted after the first. MAW is single-tenant: drop it.
        rest_routes=[r for r in create_rest_routes(handler, path_prefix=prefix) if not isinstance(r, Mount)],
    )
    return card
