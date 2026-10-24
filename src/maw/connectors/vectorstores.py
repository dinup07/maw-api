"""Vector stores for retrieval: ``memory`` (dev/tests), ``pgvector`` (internal) and ``qdrant`` (external).

Every backend implements the same small interface. ``VectorStoreConnector`` adds embedding,
chunking, metrics and the guard that a collection only ever holds vectors from one embedding model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..observability import metrics
from ..observability.tracing import span
from ..schema.workflow import VectorStoreSpec
from .embeddings import EmbeddingRegistry, embedding_label
from .mcp_client import describe_error
from .registry import ConnectionRegistry, Resolved, resolve_secrets, resolve_secrets_deep

EMBED_BATCH = 64


class VectorStoreError(RuntimeError):
    """A vector store is misconfigured or holds an incompatible collection."""


class VectorStoreUnavailable(VectorStoreError):
    """A vector store backend could not be reached or failed."""


@dataclass(frozen=True)
class CollectionInfo:
    embedding: str | None  # None when the backend can't record it (qdrant: dimensions only)
    dimensions: int


@dataclass
class Chunk:
    id: str
    document_id: str
    text: str
    metadata: dict[str, Any]


# --------------------------------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------------------------------

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _pieces(text: str, size: int) -> list[str]:
    """Split into paragraphs, then sentences, then hard slices, so no piece exceeds ``size``."""
    out: list[str] = []
    for paragraph in _PARAGRAPH.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            out.append(paragraph)
            continue
        for sentence in _SENTENCE.split(paragraph):
            while len(sentence) > size:
                out.append(sentence[:size])
                sentence = sentence[size:]
            if sentence.strip():
                out.append(sentence.strip())
    return out


def chunk_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 150) -> list[str]:
    """Pack paragraphs/sentences into chunks of at most ``chunk_size`` characters (plus overlap)."""
    if chunk_size < 50:
        raise ValueError("chunk_size must be at least 50")
    if not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")
    chunks: list[str] = []
    current = ""
    for piece in _pieces(text, chunk_size):
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= chunk_size or not current:
            current = candidate
            continue
        chunks.append(current)
        tail = current[-chunk_overlap:] if chunk_overlap else ""
        if tail and " " in tail:
            tail = tail[tail.index(" ") + 1 :]  # start the overlap on a word boundary
        current = f"{tail} {piece}".strip() if tail else piece
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------------------


class Backend(Protocol):
    async def info(self, collection: str) -> CollectionInfo | None: ...
    async def create(self, collection: str, info: CollectionInfo) -> None: ...
    async def upsert(self, collection: str, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...
    async def search(
        self, collection: str, vector: list[float], top_k: int, filter: dict[str, Any]
    ) -> list[dict[str, Any]]: ...
    async def delete_document(self, collection: str, document_id: str) -> int: ...
    async def count(self, collection: str) -> int: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class MemoryBackend:
    """Process-local collections. Lost on restart; for development and tests."""

    def __init__(self) -> None:
        self._info: dict[str, CollectionInfo] = {}
        self._rows: dict[str, dict[str, tuple[Chunk, list[float]]]] = {}

    async def info(self, collection: str) -> CollectionInfo | None:
        return self._info.get(collection)

    async def create(self, collection: str, info: CollectionInfo) -> None:
        self._info[collection] = info
        self._rows.setdefault(collection, {})

    async def upsert(self, collection: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        rows = self._rows.setdefault(collection, {})
        for chunk, vector in zip(chunks, vectors, strict=True):
            rows[chunk.id] = (chunk, vector)

    async def search(
        self, collection: str, vector: list[float], top_k: int, filter: dict[str, Any]
    ) -> list[dict[str, Any]]:
        hits = []
        for chunk, stored in self._rows.get(collection, {}).values():
            if all(chunk.metadata.get(k) == v for k, v in filter.items()):
                hits.append((_cosine(vector, stored), chunk))
        hits.sort(key=lambda pair: pair[0], reverse=True)
        return [_hit(c.id, c.document_id, c.text, c.metadata, score) for score, c in hits[:top_k]]

    async def delete_document(self, collection: str, document_id: str) -> int:
        rows = self._rows.get(collection, {})
        doomed = [cid for cid, (chunk, _) in rows.items() if chunk.document_id == document_id]
        for cid in doomed:
            del rows[cid]
        return len(doomed)

    async def count(self, collection: str) -> int:
        return len(self._rows.get(collection, {}))


def _hit(chunk_id: str, document_id: str, text: str, metadata: dict, score: float) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "document_id": document_id,
        "text": text,
        "score": round(score, 4),
        "metadata": metadata,
    }


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


PGVECTOR_SCHEMA = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE IF NOT EXISTS maw_vector_collections (
        name TEXT PRIMARY KEY, embedding TEXT NOT NULL, dimensions INT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS maw_vectors (
        collection TEXT NOT NULL REFERENCES maw_vector_collections (name) ON DELETE CASCADE,
        id TEXT NOT NULL, document_id TEXT NOT NULL, text TEXT NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb, embedding vector NOT NULL,
        PRIMARY KEY (collection, id))""",
    "CREATE INDEX IF NOT EXISTS maw_vectors_document_idx ON maw_vectors (collection, document_id)",
)


class PgVectorBackend:
    """pgvector tables in a Postgres database (MAW's own by default).

    One table holds every collection; each collection gets a partial HNSW index over
    ``embedding::vector(<dims>)``, which is also the expression the search query orders by.
    """

    def __init__(self, pool: Any):
        self.pool = pool
        self._ready = False
        self._lock = asyncio.Lock()

    async def _setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            async with self.pool.connection() as conn:
                for statement in PGVECTOR_SCHEMA:
                    try:
                        await conn.execute(statement)
                    except Exception as e:
                        raise VectorStoreError(
                            f"pgvector setup failed ({describe_error(e)}); the database needs the 'vector' "
                            "extension (e.g. the pgvector/pgvector:pg16 image)"
                        ) from e
            self._ready = True

    async def info(self, collection: str) -> CollectionInfo | None:
        await self._setup()
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT embedding, dimensions FROM maw_vector_collections WHERE name = %s", (collection,)
            )
            row = await cur.fetchone()
        return CollectionInfo(row["embedding"], row["dimensions"]) if row else None

    async def create(self, collection: str, info: CollectionInfo) -> None:
        from psycopg import sql

        await self._setup()
        index = "maw_vectors_hnsw_" + hashlib.sha256(collection.encode()).hexdigest()[:12]
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO maw_vector_collections (name, embedding, dimensions) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (collection, info.embedding, info.dimensions),
            )
            await conn.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON maw_vectors USING hnsw "
                    "((embedding::vector({})) vector_cosine_ops) WHERE collection = {}"
                ).format(sql.Identifier(index), sql.Literal(info.dimensions), sql.Literal(collection))
            )

    async def upsert(self, collection: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        from psycopg.types.json import Jsonb

        rows = [
            (collection, c.id, c.document_id, c.text, Jsonb(c.metadata), _vector_literal(v))
            for c, v in zip(chunks, vectors, strict=True)
        ]
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO maw_vectors (collection, id, document_id, text, metadata, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s::vector)
                   ON CONFLICT (collection, id) DO UPDATE SET document_id = EXCLUDED.document_id,
                   text = EXCLUDED.text, metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding""",
                rows,
            )

    async def search(
        self, collection: str, vector: list[float], top_k: int, filter: dict[str, Any]
    ) -> list[dict[str, Any]]:
        from psycopg import sql
        from psycopg.types.json import Jsonb

        dims = sql.Literal(len(vector))
        query = sql.SQL(
            "SELECT id, document_id, text, metadata, 1 - (embedding::vector({d}) <=> %(v)s::vector({d})) AS score "
            "FROM maw_vectors WHERE collection = %(c)s {where} "
            "ORDER BY embedding::vector({d}) <=> %(v)s::vector({d}) LIMIT %(k)s"
        ).format(d=dims, where=sql.SQL("AND metadata @> %(f)s") if filter else sql.SQL(""))
        params = {"v": _vector_literal(vector), "c": collection, "k": top_k, "f": Jsonb(filter)}
        async with self.pool.connection() as conn:
            cur = await conn.execute(query, params)
            rows = await cur.fetchall()
        return [_hit(r["id"], r["document_id"], r["text"], r["metadata"], float(r["score"])) for r in rows]

    async def delete_document(self, collection: str, document_id: str) -> int:
        await self._setup()
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM maw_vectors WHERE collection = %s AND document_id = %s",
                (collection, document_id),
            )
            return cur.rowcount

    async def count(self, collection: str) -> int:
        await self._setup()
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM maw_vectors WHERE collection = %s", (collection,)
            )
            return (await cur.fetchone())["n"]


class QdrantBackend:
    """Qdrant over its REST API. Qdrant records vector size but not the embedding model name."""

    def __init__(self, client: httpx.AsyncClient):
        self.http = client

    async def _call(self, method: str, path: str, body: Any = None, *, missing_ok: bool = False) -> Any:
        try:
            resp = await self.http.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise VectorStoreUnavailable(f"qdrant unreachable: {describe_error(e)}") from e
        if missing_ok and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise VectorStoreUnavailable(
                f"qdrant {method} {path} failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json().get("result")

    async def info(self, collection: str) -> CollectionInfo | None:
        result = await self._call("GET", f"/collections/{collection}", missing_ok=True)
        if result is None:
            return None
        vectors = result["config"]["params"]["vectors"]
        return CollectionInfo(None, int(vectors["size"]))

    async def create(self, collection: str, info: CollectionInfo) -> None:
        await self._call(
            "PUT", f"/collections/{collection}", {"vectors": {"size": info.dimensions, "distance": "Cosine"}}
        )
        await self._call(
            "PUT",
            f"/collections/{collection}/index?wait=true",
            {"field_name": "document_id", "field_schema": "keyword"},
        )

    @staticmethod
    def _point_id(collection: str, chunk_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"maw:{collection}:{chunk_id}"))

    async def upsert(self, collection: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        points = [
            {
                "id": self._point_id(collection, c.id),
                "vector": v,
                "payload": {"id": c.id, "document_id": c.document_id, "text": c.text, "metadata": c.metadata},
            }
            for c, v in zip(chunks, vectors, strict=True)
        ]
        await self._call("PUT", f"/collections/{collection}/points?wait=true", {"points": points})

    async def search(
        self, collection: str, vector: list[float], top_k: int, filter: dict[str, Any]
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"vector": vector, "limit": top_k, "with_payload": True}
        if filter:
            body["filter"] = {
                "must": [{"key": f"metadata.{k}", "match": {"value": v}} for k, v in filter.items()]
            }
        result = await self._call("POST", f"/collections/{collection}/points/search", body, missing_ok=True)
        hits = []
        for point in result or []:
            p = point.get("payload") or {}
            hits.append(_hit(p.get("id", str(point["id"])), p.get("document_id", ""), p.get("text", ""),
                             p.get("metadata") or {}, float(point["score"])))  # fmt: skip
        return hits

    async def delete_document(self, collection: str, document_id: str) -> int:
        doc_filter = {"must": [{"key": "document_id", "match": {"value": document_id}}]}
        counted = await self._call(
            "POST",
            f"/collections/{collection}/points/count",
            {"filter": doc_filter, "exact": True},
            missing_ok=True,
        )
        if not counted or not counted.get("count"):
            return 0
        await self._call("POST", f"/collections/{collection}/points/delete?wait=true", {"filter": doc_filter})
        return int(counted["count"])

    async def count(self, collection: str) -> int:
        result = await self._call(
            "POST", f"/collections/{collection}/points/count", {"exact": True}, missing_ok=True
        )
        return int(result["count"]) if result else 0


# --------------------------------------------------------------------------------------------------
# Connector
# --------------------------------------------------------------------------------------------------


def _document_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class VectorStoreConnector:
    def __init__(
        self, registry: ConnectionRegistry, embeddings: EmbeddingRegistry, *, default_timeout_s: float = 60
    ):
        self.registry = registry
        self.embeddings = embeddings
        self.default_timeout_s = default_timeout_s
        self.memory = MemoryBackend()
        self._internal_pool: Any = None
        self._pgvector: dict[str, PgVectorBackend] = {}
        self._external_pools: list[Any] = []
        self._qdrant_clients: dict[str, httpx.AsyncClient] = {}
        self._pool_lock = asyncio.Lock()

    def attach_pool(self, pool: Any) -> None:
        """Use MAW's own Postgres pool for ``pgvector`` stores without a ``url``."""
        self._internal_pool = pool

    async def _backend(self, store: Resolved[VectorStoreSpec]) -> Backend:
        spec = store.spec
        if spec.provider == "memory":
            return self.memory
        if spec.provider == "qdrant":
            url = resolve_secrets(spec.url or "", trusted=store.trusted).rstrip("/")
            headers = resolve_secrets_deep(spec.headers, trusted=store.trusted)
            if spec.api_key:
                headers["api-key"] = resolve_secrets(spec.api_key, trusted=store.trusted)
            timeout = spec.timeout_s or self.default_timeout_s
            key = hashlib.sha256(json.dumps([url, headers, timeout], sort_keys=True).encode()).hexdigest()
            client = self._qdrant_clients.get(key)
            if client is None:
                client = httpx.AsyncClient(
                    base_url=url, headers=headers, timeout=httpx.Timeout(timeout, connect=10)
                )
                self._qdrant_clients[key] = client
            return QdrantBackend(client)
        # pgvector
        if not spec.url:
            if self._internal_pool is None:
                raise VectorStoreError(
                    f"vector store '{store.name}' uses the internal pgvector store, which needs MAW_DATABASE_URL"
                )
            return self._pgvector.setdefault("internal", PgVectorBackend(self._internal_pool))
        dsn = resolve_secrets(spec.url, trusted=store.trusted)
        key = hashlib.sha256(dsn.encode()).hexdigest()
        async with self._pool_lock:
            if key not in self._pgvector:
                from psycopg.rows import dict_row
                from psycopg_pool import AsyncConnectionPool

                pool = AsyncConnectionPool(
                    dsn, min_size=1, max_size=5, open=False,
                    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                )  # fmt: skip
                try:
                    await pool.open(wait=True, timeout=15)
                except Exception as e:
                    raise VectorStoreUnavailable(
                        f"vector store '{store.name}' unreachable: {describe_error(e)}"
                    ) from e
                self._external_pools.append(pool)
                self._pgvector[key] = PgVectorBackend(pool)
        return self._pgvector[key]

    async def _check(
        self, store: Resolved[VectorStoreSpec], info: CollectionInfo, label: str, dims: int
    ) -> None:
        if info.dimensions != dims or (info.embedding is not None and info.embedding != label):
            have = f"{info.embedding or 'unknown model'} ({info.dimensions} dims)"
            raise VectorStoreError(
                f"vector store '{store.name}': collection '{store.spec.collection}' holds {have} vectors, "
                f"but the store is configured with {label} ({dims} dims); re-ingest into a new collection"
            )

    async def add_documents(
        self,
        store: Resolved[VectorStoreSpec],
        documents: list[dict[str, Any]],
        *,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
    ) -> dict[str, Any]:
        chunks: list[Chunk] = []
        document_ids: list[str] = []
        for doc in documents:
            text = str(doc.get("text") or "").strip()
            if not text:
                continue
            document_id = str(doc.get("id") or _document_id(text))
            metadata = dict(doc.get("metadata") or {})
            document_ids.append(document_id)
            pieces = chunk_text(text, chunk_size, chunk_overlap)
            for i, piece in enumerate(pieces):
                meta = {**metadata, "document_id": document_id, "chunk": i, "chunks": len(pieces)}
                chunks.append(Chunk(f"{document_id}#{i}", document_id, piece, meta))
        if not chunks:
            return {"documents": 0, "chunks": 0, "document_ids": []}

        backend = await self._backend(store)
        model = self.embeddings.get(store.spec.embedding, trusted=store.trusted, store=store.name)
        label = embedding_label(store.spec.embedding)
        with span("vector.upsert", store=store.name, chunks=len(chunks)):
            vectors: list[list[float]] = []
            for start in range(0, len(chunks), EMBED_BATCH):
                batch = [c.text for c in chunks[start : start + EMBED_BATCH]]
                vectors.extend(await model.aembed_documents(batch))
            info = await backend.info(store.spec.collection)
            if info is None:
                await backend.create(store.spec.collection, CollectionInfo(label, len(vectors[0])))
            else:
                await self._check(store, info, label, len(vectors[0]))
            # Re-ingesting a document replaces all of its chunks (it may now have fewer).
            for document_id in dict.fromkeys(document_ids):
                await backend.delete_document(store.spec.collection, document_id)
            await backend.upsert(store.spec.collection, chunks, vectors)
        metrics.vector_upserts_total.labels(store.name).inc(len(chunks))
        return {
            "documents": len(set(document_ids)),
            "chunks": len(chunks),
            "document_ids": list(dict.fromkeys(document_ids)),
        }

    async def search(
        self,
        store: Resolved[VectorStoreSpec],
        query: str,
        *,
        top_k: int = 4,
        min_score: float | None = None,
        filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        started = time.perf_counter()
        outcome = "error"
        try:
            with span("vector.search", store=store.name, top_k=top_k):
                backend = await self._backend(store)
                info = await backend.info(store.spec.collection)
                if info is None:
                    outcome = "ok"
                    return []  # nothing ingested yet
                model = self.embeddings.get(store.spec.embedding, trusted=store.trusted, store=store.name)
                vector = await model.aembed_query(query)
                await self._check(store, info, embedding_label(store.spec.embedding), len(vector))
                hits = await backend.search(store.spec.collection, vector, top_k, filter or {})
            if min_score is not None:
                hits = [h for h in hits if h["score"] >= min_score]
            outcome = "ok"
            return hits
        finally:
            metrics.vector_queries_total.labels(store.name, outcome).inc()
            metrics.vector_query_duration_seconds.labels(store.name).observe(time.perf_counter() - started)

    async def delete_document(self, store: Resolved[VectorStoreSpec], document_id: str) -> int:
        backend = await self._backend(store)
        return await backend.delete_document(store.spec.collection, document_id)

    async def count(self, store: Resolved[VectorStoreSpec]) -> int:
        backend = await self._backend(store)
        return await backend.count(store.spec.collection)

    async def aclose(self) -> None:
        clients, self._qdrant_clients = list(self._qdrant_clients.values()), {}
        for client in clients:
            await client.aclose()
        pools, self._external_pools = self._external_pools, []
        for pool in pools:
            await pool.close()
