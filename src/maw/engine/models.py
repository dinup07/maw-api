"""Chat model resolution (any ``init_chat_model`` provider) and the deterministic scripted model."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

from ..connectors.registry import ConnectionRegistry, resolve_secrets
from ..schema.workflow import ModelSpec, WorkflowDefinition


class ModelConfigError(RuntimeError):
    """A model reference or its configuration is invalid."""


# --------------------------------------------------------------------------------------------------
# ScriptedChatModel
# --------------------------------------------------------------------------------------------------


def _message_text(msg: BaseMessage) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


class ScriptedChatModel(BaseChatModel):
    """Deterministic model for tests and demos. With no rules or responses it echoes.

    ``rules``: ``[{"contains": str | [str], "respond": response | [responses]}]``; the first rule
    whose needle occurs (case-insensitively) in the last human message wins, else ``responses``.
    A list of responses is stepped through by the number of AI messages since that human message,
    which walks a tool loop forward.
    """

    rules: list[dict[str, Any]] = []
    responses: Any = None
    model_name: str = "scripted"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _pick(self, messages: list[BaseMessage]) -> tuple[Any, str]:
        human_text, step = "", 0
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                human_text = _message_text(msg)
                break
            if isinstance(msg, AIMessage):
                step += 1
        chosen = self.responses
        lowered = human_text.lower()
        for rule in self.rules:
            needles = rule.get("contains", [])
            needles = [needles] if isinstance(needles, str) else needles
            if any(str(n).lower() in lowered for n in needles):
                chosen = rule.get("respond")
                break
        if isinstance(chosen, list):
            chosen = chosen[min(step, len(chosen) - 1)] if chosen else None
        return chosen, human_text

    @staticmethod
    def _to_message(response: Any, human_text: str) -> AIMessage:
        if response is None:
            return AIMessage(content=f"echo: {human_text}")
        if isinstance(response, str):
            return AIMessage(content=response)
        if isinstance(response, dict) and "tool_calls" in response:
            calls = [
                {"name": c["name"], "args": c.get("args", {}), "id": f"call_{i}", "type": "tool_call"}
                for i, c in enumerate(response["tool_calls"])
            ]
            return AIMessage(content=response.get("content", ""), tool_calls=calls)
        if isinstance(response, dict) and "content" in response:
            return AIMessage(content=response["content"])
        return AIMessage(content=json.dumps(response))

    def _respond(self, messages: list[BaseMessage]) -> ChatResult:
        response, human_text = self._pick(messages)
        message = self._to_message(response, human_text)
        prompt_chars = sum(len(_message_text(m)) for m in messages)
        out_tokens = len(_message_text(message)) // 4
        message.usage_metadata = {
            "input_tokens": prompt_chars // 4,
            "output_tokens": out_tokens,
            "total_tokens": prompt_chars // 4 + out_tokens,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._respond(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._respond(messages)

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:  # type: ignore[override]
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:  # type: ignore[override]
        def structured(messages: Any) -> Any:
            msgs = messages.to_messages() if hasattr(messages, "to_messages") else list(messages)
            response, human_text = self._pick(msgs)
            if isinstance(response, dict) and "structured" in response:
                return response["structured"]
            if isinstance(response, dict) and "tool_calls" not in response and "content" not in response:
                return response
            text = response if isinstance(response, str) else self._to_message(response, human_text).content
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, ValueError):
                pass
            return {_first_string_property(schema): text}

        return RunnableLambda(structured)


def _first_string_property(schema: Any) -> str:
    if isinstance(schema, dict):
        for name, prop in (schema.get("properties") or {}).items():
            if isinstance(prop, dict) and prop.get("type") == "string":
                return name
    return "text"


# --------------------------------------------------------------------------------------------------
# ModelRegistry
# --------------------------------------------------------------------------------------------------


def _key(parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class ModelRegistry:
    def __init__(
        self, connections: ConnectionRegistry, *, allow_inline_api_keys: bool = False, cache_size: int = 64
    ):
        self.connections = connections
        self.allow_inline_api_keys = allow_inline_api_keys
        self.cache_size = cache_size
        self._cache: OrderedDict[str, BaseChatModel] = OrderedDict()

    @staticmethod
    def resolve_spec(ref: str, wf: WorkflowDefinition) -> tuple[str, ModelSpec | None, str | None, str]:
        """Return ``(name, spec, provider, model)`` for a node's ``model`` reference."""
        if ref in wf.models:
            spec = wf.models[ref]
            return ref, spec, spec.provider, spec.model
        if ref == "scripted":
            return ref, None, "scripted", "scripted"
        if ":" in ref:
            provider, model = ref.split(":", 1)
            return ref, None, provider, model
        return ref, None, None, ref

    def get(self, ref: str, wf: WorkflowDefinition) -> tuple[BaseChatModel, str]:
        name, spec, provider, model_name = self.resolve_spec(ref, wf)
        label = f"{provider or 'auto'}:{model_name}"

        if provider == "scripted":
            params = dict(spec.params) if spec else {}
            return self._cached(_key(["scripted", params]), lambda: ScriptedChatModel(**params)), label

        kwargs: dict[str, Any] = dict(spec.params) if spec else {}
        if spec is not None:
            if spec.temperature is not None:
                kwargs["temperature"] = spec.temperature
            if spec.max_tokens is not None:
                kwargs["max_tokens"] = spec.max_tokens
            if spec.base_url:
                base_url = resolve_secrets(spec.base_url, trusted=False)
                self.connections.check_url(base_url)
                kwargs["base_url"] = base_url
            if spec.api_key:
                if "${" not in spec.api_key and not self.allow_inline_api_keys:
                    raise ModelConfigError(
                        f"model '{name}': literal api_key is disabled; use '${{secret:NAME}}' "
                        "or set MAW_ALLOW_INLINE_API_KEYS=true"
                    )
                kwargs["api_key"] = resolve_secrets(spec.api_key, trusted=False)

        def build() -> BaseChatModel:
            from langchain.chat_models import init_chat_model

            try:
                return init_chat_model(model_name, model_provider=provider or None, **kwargs)
            except Exception as e:  # missing provider package, unknown provider, missing credentials, …
                raise ModelConfigError(f"model '{name}': {type(e).__name__}: {e}") from e

        return self._cached(_key([provider, model_name, kwargs]), build), label

    def _cached(self, key: str, build: Any) -> BaseChatModel:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        model = build()
        self._cache[key] = model
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return model
