"""FastAPI application factory and the engine wiring shared with the tests.

``create_app`` (the HTTP API and lifespan) is added with the API layer.
"""

from __future__ import annotations

from ..connectors import A2AConnector, ConnectionRegistry, McpConnector
from ..engine.models import ModelRegistry
from ..engine.nodes import EngineContext
from ..settings import Settings


def build_engine_context(settings: Settings) -> EngineContext:
    """Connections from the trusted file, the model registry and both outbound connectors."""
    connections = ConnectionRegistry.from_file(settings.connections_file, settings.allowed_outbound_hosts)
    return EngineContext(
        connections=connections,
        models=ModelRegistry(connections, allow_inline_api_keys=settings.allow_inline_api_keys),
        mcp=McpConnector(
            tool_cache_ttl_s=settings.mcp_tool_cache_ttl_s, default_timeout_s=settings.outbound_timeout_s
        ),
        a2a=A2AConnector(
            connections,
            card_ttl_s=settings.a2a_card_cache_ttl_s,
            default_timeout_s=settings.outbound_timeout_s,
        ),
        default_max_tool_calls=settings.default_max_tool_calls,
        default_max_node_visits=settings.default_max_node_visits,
    )
