"""Outbound connections: the registry (secrets, allowlist), MCP and A2A clients."""

from .a2a_client import A2ACallError, A2AConnector, A2AResult
from .mcp_client import McpConnector, McpToolError
from .registry import ConnectionError_, ConnectionRegistry, Resolved, resolve_secrets, resolve_secrets_deep

__all__ = [
    "A2ACallError",
    "A2AConnector",
    "A2AResult",
    "ConnectionError_",
    "ConnectionRegistry",
    "McpConnector",
    "McpToolError",
    "Resolved",
    "resolve_secrets",
    "resolve_secrets_deep",
]
