"""Outbound connections: the registry (secrets, allowlist), MCP and A2A clients."""

from .a2a_client import A2ACallError, A2AConnector, A2AResult
from .embeddings import EmbeddingConfigError, EmbeddingRegistry, ScriptedEmbeddings
from .mcp_client import McpConnector, McpToolError
from .registry import ConnectionError_, ConnectionRegistry, Resolved, resolve_secrets, resolve_secrets_deep
from .vectorstores import VectorStoreConnector, VectorStoreError, chunk_text

__all__ = [
    "A2ACallError",
    "A2AConnector",
    "A2AResult",
    "ConnectionError_",
    "ConnectionRegistry",
    "EmbeddingConfigError",
    "EmbeddingRegistry",
    "McpConnector",
    "McpToolError",
    "Resolved",
    "ScriptedEmbeddings",
    "VectorStoreConnector",
    "VectorStoreError",
    "chunk_text",
    "resolve_secrets",
    "resolve_secrets_deep",
]
