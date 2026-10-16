"""Embedding models: any ``init_embeddings`` provider, plus a deterministic ``scripted`` model."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from typing import Any

from langchain_core.embeddings import Embeddings

from ..schema.workflow import EmbeddingSpec
from .registry import ConnectionError_, ConnectionRegistry, resolve_secrets

_WORD = re.compile(r"[a-z0-9]+")
# Words that carry no topic; without this, any two English texts look a little similar.
_STOPWORDS = frozenset(
    "the and for are but not you your yours with what when where which who whom why how this that these "
    "those there their them they then than from into onto over under was were been being have has had "
    "does did doing can could should would will shall may might must about above after again all any "
    "because before below between both each few more most other some such only own same too very just "
    "our ours out off once here also get got its it's one two per via ever".split()
)


class EmbeddingConfigError(ConnectionError_):
    """An embedding model reference or its configuration is invalid."""


class ScriptedEmbeddings(Embeddings):
    """Deterministic hashed bag-of-words embeddings (no network, no keys).

    Texts sharing words get similar vectors, which is enough for tests and demos. Not semantic.
    """

    def __init__(self, dimensions: int = 256):
        self.dimensions = dimensions

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in _WORD.findall(text.lower()):
            if len(word) < 3 or word in _STOPWORDS:
                continue
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0 if digest[4] % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def embedding_label(spec: EmbeddingSpec) -> str:
    """Identifies the embedding space; a collection must only ever see one."""
    dims = spec.params.get("dimensions") or spec.params.get("output_dimensionality")
    return f"{spec.provider}:{spec.model}" + (f"@{dims}" if dims else "")


class EmbeddingRegistry:
    def __init__(
        self, connections: ConnectionRegistry, *, allow_inline_api_keys: bool = False, cache_size: int = 32
    ):
        self.connections = connections
        self.allow_inline_api_keys = allow_inline_api_keys
        self.cache_size = cache_size
        self._cache: OrderedDict[str, Embeddings] = OrderedDict()

    def get(self, spec: EmbeddingSpec, *, trusted: bool, store: str) -> Embeddings:
        if spec.provider == "scripted":
            dims = int(spec.params.get("dimensions", 256))
            return self._cached(f"scripted:{dims}", lambda: ScriptedEmbeddings(dims))

        kwargs: dict[str, Any] = dict(spec.params)
        if spec.base_url:
            base_url = resolve_secrets(spec.base_url, trusted=trusted)
            self.connections.check_url(base_url)
            kwargs["base_url"] = base_url
        if spec.api_key:
            if "${" not in spec.api_key and not trusted and not self.allow_inline_api_keys:
                raise EmbeddingConfigError(
                    f"vector store '{store}': literal embedding api_key is disabled; use '${{secret:NAME}}'"
                )
            kwargs["api_key"] = resolve_secrets(spec.api_key, trusted=trusted)

        def build() -> Embeddings:
            from langchain.embeddings import init_embeddings

            try:
                return init_embeddings(spec.model, provider=spec.provider, **kwargs)
            except Exception as e:  # missing provider package, unknown provider, missing credentials
                raise EmbeddingConfigError(
                    f"vector store '{store}' embedding: {type(e).__name__}: {e}"
                ) from e

        key = hashlib.sha256(
            json.dumps([spec.provider, spec.model, kwargs], sort_keys=True, default=str).encode()
        )
        return self._cached(key.hexdigest(), build)

    def _cached(self, key: str, build: Any) -> Embeddings:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        model = build()
        self._cache[key] = model
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return model
