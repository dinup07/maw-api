"""A2A connector: agent-card discovery (cached, allowlist-checked) and message sending."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import get_data_parts, get_text_parts, new_data_part, new_text_part
from a2a.types import AgentCard, Message, Role, SendMessageRequest, StreamResponse, TaskState

from ..observability import metrics
from ..observability.tracing import span
from ..schema.workflow import AgentSpec
from .mcp_client import describe_error
from .registry import ConnectionRegistry, Resolved, resolve_secrets_deep

STATE_NAMES = {
    TaskState.TASK_STATE_SUBMITTED: "submitted",
    TaskState.TASK_STATE_WORKING: "working",
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "canceled",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
}
FAILED_STATES = frozenset({"failed", "rejected", "canceled", "auth_required"})


class A2ACallError(RuntimeError):
    """A remote agent was unreachable or ended in a failure state."""


@dataclass
class A2AResult:
    texts: list[str] = field(default_factory=list)
    data: list[Any] = field(default_factory=list)
    state: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    status_text: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(t for t in self.texts if t) or self.status_text or ""

    def as_dict(self) -> dict[str, Any]:
        data: Any = None
        if len(self.data) == 1:
            data = self.data[0]
        elif self.data:
            data = list(self.data)
        return {
            "text": self.text,
            "data": data,
            "state": self.state,
            "task_id": self.task_id,
            "context_id": self.context_id,
        }


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _absorb(result: A2AResult, resp: StreamResponse) -> None:
    kind = resp.WhichOneof("payload")
    if kind == "message":
        msg = resp.message
        result.state = "completed"
        result.texts.extend(get_text_parts(msg.parts))
        result.data.extend(get_data_parts(msg.parts))
        result.task_id = msg.task_id or result.task_id
        result.context_id = msg.context_id or result.context_id
    elif kind == "task":
        task = resp.task
        result.task_id = task.id or result.task_id
        result.context_id = task.context_id or result.context_id
        result.state = STATE_NAMES.get(task.status.state, result.state)
        if task.artifacts:
            result.texts = [t for a in task.artifacts for t in get_text_parts(a.parts)]
            result.data = [d for a in task.artifacts for d in get_data_parts(a.parts)]
        if task.status.HasField("message"):
            result.status_text = "\n".join(get_text_parts(task.status.message.parts)) or result.status_text
    elif kind == "status_update":
        update = resp.status_update
        result.task_id = update.task_id or result.task_id
        result.context_id = update.context_id or result.context_id
        result.state = STATE_NAMES.get(update.status.state, result.state)
        if update.status.HasField("message"):
            result.status_text = "\n".join(get_text_parts(update.status.message.parts)) or result.status_text
    elif kind == "artifact_update":
        update = resp.artifact_update
        result.task_id = update.task_id or result.task_id
        result.context_id = update.context_id or result.context_id
        texts = get_text_parts(update.artifact.parts)
        if update.append and result.texts and texts:
            result.texts[-1] += texts[0]
            result.texts.extend(texts[1:])
        else:
            result.texts.extend(texts)
        result.data.extend(get_data_parts(update.artifact.parts))


class A2AConnector:
    def __init__(
        self, registry: ConnectionRegistry, *, card_ttl_s: float = 300, default_timeout_s: float = 60
    ):
        self.registry = registry
        self.card_ttl_s = card_ttl_s
        self.default_timeout_s = default_timeout_s
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._cards: dict[str, tuple[float, AgentCard]] = {}

    def _http(self, agent: Resolved[AgentSpec]) -> httpx.AsyncClient:
        headers = resolve_secrets_deep(agent.spec.headers, trusted=agent.trusted)
        timeout = agent.spec.timeout_s or self.default_timeout_s
        key = _hash(agent.spec.url, headers, timeout)
        client = self._clients.get(key)
        if client is None:
            client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout, connect=10))
            self._clients[key] = client
        return client

    async def card(self, agent: Resolved[AgentSpec]) -> AgentCard:
        headers = resolve_secrets_deep(agent.spec.headers, trusted=agent.trusted)
        key = _hash(agent.spec.url, agent.spec.card_path, headers)
        cached = self._cards.get(key)
        if cached is not None and self.card_ttl_s > 0 and time.monotonic() - cached[0] <= self.card_ttl_s:
            return cached[1]
        with span("a2a.get_card", agent=agent.name):
            try:
                resolver = A2ACardResolver(self._http(agent), agent.spec.url.rstrip("/"))
                card = await resolver.get_agent_card(relative_card_path=agent.spec.card_path)
            except Exception as e:
                raise A2ACallError(f"agent '{agent.name}' unreachable: {describe_error(e)}") from e
        # The card decides where messages really go, so its endpoints must pass the allowlist too.
        for iface in card.supported_interfaces:
            self.registry.check_url(iface.url)
        if self.card_ttl_s > 0:
            self._cards[key] = (time.monotonic(), card)
        return card

    async def send(
        self,
        agent: Resolved[AgentSpec],
        text: str,
        *,
        data: Any = None,
        context_id: str | None = None,
        streaming: bool = False,
    ) -> A2AResult:
        started = time.perf_counter()
        outcome = "error"
        try:
            card = await self.card(agent)
            client = ClientFactory(ClientConfig(streaming=streaming, httpx_client=self._http(agent))).create(
                card
            )
            parts = ([new_text_part(text)] if text else []) + (
                [new_data_part(data)] if data is not None else []
            )
            message = Message(message_id=uuid.uuid4().hex, role=Role.ROLE_USER, parts=parts)
            if context_id:
                message.context_id = context_id
            result = A2AResult()
            with span("a2a.send_message", agent=agent.name, streaming=streaming):
                try:
                    async for resp in client.send_message(SendMessageRequest(message=message)):
                        _absorb(result, resp)
                except A2ACallError:
                    raise
                except Exception as e:
                    reachable = not _caused_by(e, httpx.TransportError)
                    verb = "call failed" if reachable else "unreachable"
                    raise A2ACallError(f"agent '{agent.name}' {verb}: {describe_error(e)}") from e
                if result.state in FAILED_STATES:
                    raise A2ACallError(
                        f"agent '{agent.name}' ended in state '{result.state}': {result.status_text or result.text}"
                    )
            outcome = "ok"
            return result
        finally:
            metrics.a2a_calls_total.labels(agent.name, outcome).inc()
            metrics.a2a_call_duration_seconds.labels(agent.name).observe(time.perf_counter() - started)

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()


def _caused_by(exc: BaseException, kind: type[BaseException]) -> bool:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return True
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                stack.append(linked)
    return False
