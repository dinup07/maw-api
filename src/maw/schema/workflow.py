"""The workflow contract: Pydantic models for a MAW workflow definition.

Every model forbids unknown fields, so a typo such as ``sytem_prompt`` fails validation instead of
being silently ignored.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

START = "start"
END = "end"
RESERVED_IDS = frozenset({START, END})

ID_PATTERN = r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------------------
# Models and connections
# --------------------------------------------------------------------------------------------------


class ModelSpec(_Model):
    """A named chat model. ``provider`` is any ``init_chat_model`` provider, or ``scripted``."""

    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    params: dict[str, Any] = {}


class McpServerSpec(_Model):
    transport: Literal["streamable_http", "sse", "stdio"] = "streamable_http"
    url: str | None = None
    headers: dict[str, str] = {}
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    timeout_s: float | None = None

    @model_validator(mode="after")
    def _check_transport(self) -> McpServerSpec:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio MCP servers need 'command'")
        elif not self.url:
            raise ValueError(f"{self.transport} MCP servers need 'url'")
        return self


class EmbeddingSpec(_Model):
    """An embedding model. ``provider`` is any ``init_embeddings`` provider, or ``scripted``."""

    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    params: dict[str, Any] = {}  # e.g. {"dimensions": 768} (OpenAI), {"output_dimensionality": 768} (Gemini)


class VectorStoreSpec(_Model):
    """A vector database collection.

    ``pgvector`` without ``url`` uses MAW's own Postgres (the internal store); ``qdrant`` is an
    external service; ``memory`` is process-local, for development and tests.
    """

    provider: Literal["pgvector", "qdrant", "memory"]
    collection: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,63}$")
    embedding: EmbeddingSpec
    url: str | None = None  # qdrant base URL, or a Postgres DSN for an external pgvector database
    api_key: str | None = None
    headers: dict[str, str] = {}
    timeout_s: float | None = None
    description: str | None = None  # tool description when an LLM searches this store

    @model_validator(mode="after")
    def _check_url(self) -> VectorStoreSpec:
        if self.provider == "qdrant" and not self.url:
            raise ValueError("qdrant vector stores need 'url'")
        return self


class AgentSpec(_Model):
    url: str
    card_path: str | None = None
    headers: dict[str, str] = {}
    timeout_s: float | None = None
    description: str | None = None


# --------------------------------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------------------------------


class RetryPolicy(_Model):
    max_attempts: int = Field(1, ge=1, le=10)
    backoff_s: float = Field(0.5, ge=0)


class _NodeBase(_Model):
    id: str = Field(pattern=ID_PATTERN)
    description: str | None = None
    write_output: bool = True
    retry: RetryPolicy = RetryPolicy()
    timeout_s: float | None = Field(None, gt=0)
    on_error: Literal["fail", "continue"] = "fail"
    defer: bool = False
    max_visits: int | None = Field(None, ge=1)

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_IDS:
            raise ValueError(f"'{v}' is a reserved id")
        return v


class McpToolRef(_Model):
    server: str
    allow: list[str] = []
    block: list[str] = []


class LlmTools(_Model):
    mcp: list[str | McpToolRef] = []
    agents: list[str] = []
    functions: list[str] = []
    retrievers: list[str] = []  # vector stores the model can search

    def mcp_refs(self) -> list[McpToolRef]:
        return [McpToolRef(server=r) if isinstance(r, str) else r for r in self.mcp]

    def is_empty(self) -> bool:
        return not (self.mcp or self.agents or self.functions or self.retrievers)


class LlmNode(_NodeBase):
    type: Literal["llm"]
    model: str
    system_prompt: str | None = None
    user_message: str | None = None
    inputs: dict[str, Any] = {}
    tools: LlmTools = LlmTools()
    output_schema: dict[str, Any] | None = None
    recover_text_field: str | None = None
    history: Literal["none", "all"] | Annotated[int, Field(ge=0)] = "none"
    max_tool_calls: int | None = Field(None, ge=0)
    append_to_messages: bool = True


class McpNode(_NodeBase):
    type: Literal["mcp"]
    server: str
    tool: str
    arguments: dict[str, Any] = {}


class A2aNode(_NodeBase):
    type: Literal["a2a"]
    agent: str
    message: str | None = None
    data: dict[str, Any] | None = None
    inputs: dict[str, Any] = {}
    streaming: bool = False
    keep_context: bool = True
    append_to_messages: bool = True


class FunctionNode(_NodeBase):
    type: Literal["function"]
    function: str
    arguments: dict[str, Any] = {}


class TransformNode(_NodeBase):
    type: Literal["transform"]
    mapping: dict[str, Any]


class HumanNode(_NodeBase):
    type: Literal["human"]
    prompt: str = "Input required"
    inputs: dict[str, Any] = {}


class RagNode(_NodeBase):
    """Retrieve relevant passages from a vector store for later nodes."""

    type: Literal["rag"]
    store: str
    query: str | None = None  # template; defaults to the run input text
    inputs: dict[str, Any] = {}
    top_k: int = Field(4, ge=1, le=50)
    min_score: float | None = None  # drop hits below this similarity (cosine, -1..1)
    filter: dict[str, Any] = {}  # metadata equality filter; values are value expressions
    max_context_chars: int = Field(8000, ge=100)


Node = Annotated[
    LlmNode | McpNode | A2aNode | FunctionNode | TransformNode | HumanNode | RagNode,
    Field(discriminator="type"),
]

NODE_TYPES = ("llm", "mcp", "a2a", "function", "transform", "human", "rag")


# --------------------------------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------------------------------

Operator = Literal[
    "equals", "not_equals", "in", "not_in", "contains", "gt", "gte", "lt", "lte",
    "exists", "not_exists", "matches",
]  # fmt: skip
Target = str | list[str]


def _flatten(target: Target | None) -> list[str]:
    if target is None:
        return []
    return [target] if isinstance(target, str) else list(target)


class Condition(_Model):
    field: str
    operator: Operator = "equals"
    value: Any = None
    target: Target


class Edge(_Model):
    source: str
    target: Target | None = None
    condition: list[Condition] | None = None

    @model_validator(mode="after")
    def _needs_target(self) -> Edge:
        if self.target is None and not self.condition:
            raise ValueError("an edge needs 'target' or a non-empty 'condition'")
        return self

    def all_targets(self) -> list[str]:
        out = _flatten(self.target)
        for cond in self.condition or []:
            out.extend(_flatten(cond.target))
        return out


# --------------------------------------------------------------------------------------------------
# Limits, exposure and the top-level definition
# --------------------------------------------------------------------------------------------------


class Limits(_Model):
    recursion_limit: int | None = Field(None, ge=1)
    max_node_visits: int | None = Field(None, ge=1)


class SkillSpec(_Model):
    id: str
    name: str
    description: str
    tags: list[str] = []
    examples: list[str] = []


class ExposeSpec(_Model):
    a2a: bool = False
    name: str | None = None
    description: str | None = None
    skills: list[SkillSpec] = []
    streaming: bool = True


class WorkflowDefinition(_Model):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, title="WorkflowDefinition")

    id: str = Field("inline", pattern=ID_PATTERN)
    version: str | None = None
    description: str | None = None
    models: dict[str, ModelSpec] = {}
    mcp_servers: dict[str, McpServerSpec] = {}
    agents: dict[str, AgentSpec] = {}
    vector_stores: dict[str, VectorStoreSpec] = {}
    nodes: list[Node]
    edges: list[Edge]
    limits: Limits = Limits()
    expose: ExposeSpec = ExposeSpec()

    def node_map(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def content_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
