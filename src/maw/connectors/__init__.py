"""Outbound connections: the registry (secrets, allowlist), MCP and A2A clients."""

from .registry import ConnectionError_, ConnectionRegistry, Resolved, resolve_secrets, resolve_secrets_deep

__all__ = ["ConnectionError_", "ConnectionRegistry", "Resolved", "resolve_secrets", "resolve_secrets_deep"]
