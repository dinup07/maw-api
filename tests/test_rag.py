"""RAG: chunking, embeddings, the three vector-store backends, the rag node, retriever tools, API and CLI.

pgvector tests need MAW_TEST_DATABASE_URL (with the 'vector' extension available); qdrant tests need
MAW_TEST_QDRANT_URL. Both are skipped otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
from conftest import REPO_ROOT, ServerThread, build_service, chain, free_port, make_settings, wf

from maw.connectors import (
    ConnectionError_,
    ConnectionRegistry,
    EmbeddingConfigError,
    EmbeddingRegistry,
    ScriptedEmbeddings,
    VectorStoreConnector,
    VectorStoreError,
    chunk_text,
)
from maw.engine.nodes.rag import build_context
from maw.schema import EmbeddingSpec, VectorStoreSpec, WorkflowValidationError, parse_workflow, validate_graph

DOCS_DIR = REPO_ROOT / "examples" / "docs"
SCRIPTED = {"provider": "scripted", "model": "hashing-256"}
DOCS = [
    {"id": "vpn", "text": "To reset your VPN password open the self-service portal and choose VPN.",
     "metadata": {"team": "it", "source": "vpn.md"}},
    {"id": "leave", "text": "Full-time staff accrue 24 days of annual leave per year.",
     "metadata": {"team": "hr", "source": "leave.md"}},
    {"id": "refund", "text": "Duplicate invoice charges are refunded within 3 business days.",
     "metadata": {"team": "billing", "source": "refund.md"}},
]  # fmt: skip


# --------------------------------------------------------------------------------------------------
# Chunking, embeddings, context
# --------------------------------------------------------------------------------------------------


def test_chunk_text_packs_paragraphs_with_overlap():
    assert chunk_text("Short text.", 100, 10) == ["Short text."]
    paragraphs = [f"Paragraph {i} " + "word " * 30 for i in range(6)]
    chunks = chunk_text("\n\n".join(paragraphs), chunk_size=400, chunk_overlap=60)
    assert len(chunks) > 1
    assert all(len(c) <= 400 + 60 + 1 for c in chunks)
    assert all(p.strip()[:12] in "".join(chunks) for p in paragraphs)  # nothing lost
    assert chunks[1].split()[0] == "word"  # starts with overlap from the previous chunk
    long_sentence = "x" * 1200
    assert all(len(c) <= 500 for c in chunk_text(long_sentence, 500, 0))
    for size, overlap in ((10, 0), (100, 100), (100, -1)):
        with pytest.raises(ValueError):
            chunk_text("text", size, overlap)


def test_scripted_embeddings_are_deterministic_and_topical():
    emb = ScriptedEmbeddings(256)
    a, b = emb.embed_query("reset my vpn password"), emb.embed_query("reset my vpn password")
    assert a == b and len(a) == 256
    assert abs(sum(v * v for v in a) - 1) < 1e-9

    def cos(x, y):
        return sum(p * q for p, q in zip(x, y, strict=True))

    vpn = emb.embed_documents(["VPN password reset in the self-service portal"])[0]
    leave = emb.embed_documents(["annual leave accrual policy"])[0]
    assert cos(a, vpn) > cos(a, leave)
    assert emb.embed_query("the and with what") == [0.0] * 256  # stop words carry no signal


def test_build_context_numbers_and_truncates():
    docs = [{"text": "alpha " * 20, "metadata": {"source": "a.md"}}, {"text": "beta " * 200, "id": "b#0"}]
    context = build_context(docs, max_chars=400)
    assert context.startswith("[1] (a.md) alpha")
    assert "[2] (b#0) beta" in context and context.endswith("…")
    assert len(context) <= 400 + 10
    assert build_context([]) == ""


# --------------------------------------------------------------------------------------------------
# Contract and trust rules
# --------------------------------------------------------------------------------------------------


def test_rag_node_and_store_references_validate():
    raw = wf(
        [
            {"id": "r", "type": "rag", "store": "docs", "top_k": 2, "filter": {"team": "input.team"}},
            {"id": "a", "type": "llm", "model": "scripted", "tools": {"retrievers": ["docs"]}},
        ],
        chain("r", "a"),
    )
    definition, _ = parse_workflow(raw)
    validate_graph(definition, known_vector_stores=["docs"])
    with pytest.raises(WorkflowValidationError) as exc:
        validate_graph(definition)
    assert "node 'r': unknown vector store 'docs'" in exc.value.errors
    assert "node 'a': unknown vector store 'docs'" in exc.value.errors


@pytest.mark.parametrize(
    ("store", "message"),
    [
        ({"provider": "qdrant", "collection": "c", "embedding": SCRIPTED}, "qdrant vector stores need 'url'"),
        ({"provider": "pgvector", "collection": "bad name!", "embedding": SCRIPTED}, "collection"),
        ({"provider": "weaviate", "collection": "c", "embedding": SCRIPTED}, "provider"),
        ({"provider": "memory", "collection": "c"}, "embedding"),
    ],
)
def test_invalid_vector_store_specs(store, message):
    raw = wf([{"id": "a", "type": "transform", "mapping": {}}], chain("a"), vector_stores={"s": store})
    with pytest.raises(WorkflowValidationError) as exc:
        parse_workflow(raw)
    assert message in "\n".join(exc.value.errors)


def test_inline_store_trust_rules():
    registry = ConnectionRegistry(["127.0.0.1"])
    inline, _ = parse_workflow(
        wf(
            [{"id": "a", "type": "transform", "mapping": {}}],
            chain("a"),
            vector_stores={
                "mem": {"provider": "memory", "collection": "c", "embedding": SCRIPTED},
                "pg": {"provider": "pgvector", "collection": "c", "embedding": SCRIPTED},
                "ok": {
                    "provider": "qdrant",
                    "collection": "c",
                    "url": "http://127.0.0.1:6333",
                    "embedding": SCRIPTED,
                },
                "evil": {
                    "provider": "qdrant",
                    "collection": "c",
                    "url": "http://evil.example",
                    "embedding": SCRIPTED,
                },
                "evil_embed": {
                    "provider": "qdrant",
                    "collection": "c",
                    "url": "http://127.0.0.1:6333",
                    "embedding": {"provider": "openai", "model": "m", "base_url": "http://evil.example/v1"},
                },
            },  # fmt: skip
        )
    )
    for name in ("mem", "pg"):
        with pytest.raises(ConnectionError_, match="only be defined in the server-side connections file"):
            registry.vector_store(name, inline)
    assert registry.vector_store("ok", inline).trusted is False
    for name in ("evil", "evil_embed"):
        with pytest.raises(ConnectionError_, match="evil.example"):
            registry.vector_store(name, inline)
    with pytest.raises(ConnectionError_, match="unknown vector store 'nope'"):
        registry.vector_store("nope", inline)


def test_describe_hides_pgvector_dsn_and_literal_embedding_keys_are_refused():
    registry = ConnectionRegistry(
        vector_stores={
            "ext": VectorStoreSpec(
                provider="pgvector",
                collection="c",
                url="postgresql://u:secret@db/x",
                embedding=EmbeddingSpec(**SCRIPTED),
            ),  # fmt: skip
        }
    )
    described = registry.describe()["vector_stores"]["ext"]
    assert described["url"] is None and "secret" not in json.dumps(described)
    embeddings = EmbeddingRegistry(registry)
    literal = EmbeddingSpec(provider="openai", model="text-embedding-3-small", api_key="sk-pasted")
    with pytest.raises(EmbeddingConfigError, match="literal embedding api_key is disabled"):
        embeddings.get(literal, trusted=False, store="s")
    assert embeddings.get(literal, trusted=True, store="s") is not None  # trusted file may hold it


# --------------------------------------------------------------------------------------------------
# Backends (same behaviour for memory, pgvector and qdrant)
# --------------------------------------------------------------------------------------------------


@pytest.fixture(params=["memory", "pgvector", "qdrant"])
async def backend(request):
    """Yields (connector, registry, make_store) for one backend, with a unique collection per test."""
    provider = request.param
    collection = f"t_{uuid.uuid4().hex[:10]}"
    async with AsyncExitStack() as stack:
        url = None
        pool = None
        if provider == "pgvector":
            dsn = os.environ.get("MAW_TEST_DATABASE_URL")
            if not dsn:
                pytest.skip("MAW_TEST_DATABASE_URL is not set")
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            pool = AsyncConnectionPool(
                dsn, open=False, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
            )
            await pool.open()
            stack.push_async_callback(pool.close)
            try:
                async with pool.connection() as conn:
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as e:
                pytest.skip(f"pgvector extension unavailable: {e}")
        elif provider == "qdrant":
            url = os.environ.get("MAW_TEST_QDRANT_URL")
            if not url:
                pytest.skip("MAW_TEST_QDRANT_URL is not set")

        def make_store(name="s", **embedding):
            spec = VectorStoreSpec(
                provider=provider,
                collection=collection,
                url=url,
                embedding=EmbeddingSpec(**(embedding or SCRIPTED)),
            )
            return spec

        registry = ConnectionRegistry(vector_stores={"s": make_store()})
        connector = VectorStoreConnector(registry, EmbeddingRegistry(registry))
        if pool is not None:
            connector.attach_pool(pool)
        stack.push_async_callback(connector.aclose)
        yield connector, registry, make_store
        if provider == "qdrant":
            async with httpx.AsyncClient() as http:
                await http.delete(f"{url}/collections/{collection}")
        elif pool is not None:
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM maw_vector_collections WHERE name = %s", (collection,))


async def test_backend_ingest_search_filter_delete(backend):
    connector, registry, _ = backend
    store = registry.vector_store("s")
    assert await connector.search(store, "vpn password") == []  # nothing ingested yet
    assert await connector.count(store) == 0

    summary = await connector.add_documents(store, DOCS)
    assert summary == {"documents": 3, "chunks": 3, "document_ids": ["vpn", "leave", "refund"]}
    assert await connector.count(store) == 3

    hits = await connector.search(store, "how do I reset my vpn password", top_k=2)
    assert [h["document_id"] for h in hits][0] == "vpn"
    assert hits[0]["metadata"]["team"] == "it" and hits[0]["metadata"]["chunk"] == 0
    assert hits[0]["score"] > 0.3 and hits[0]["id"] == "vpn#0"
    assert len(hits) == 2 and hits[0]["score"] >= hits[1]["score"]

    filtered = await connector.search(store, "vpn password", filter={"team": "hr"})
    assert [h["document_id"] for h in filtered] == ["leave"]
    assert await connector.search(store, "vpn password", min_score=0.99) == []

    assert await connector.delete_document(store, "vpn") == 1
    assert await connector.delete_document(store, "vpn") == 0
    assert await connector.count(store) == 2


async def test_backend_reingest_replaces_document_chunks(backend):
    connector, registry, _ = backend
    store = registry.vector_store("s")
    long_text = "\n\n".join(f"Section {i}: " + "policy detail " * 20 for i in range(5))
    first = await connector.add_documents(
        store, [{"id": "handbook", "text": long_text}], chunk_size=300, chunk_overlap=0
    )
    assert first["chunks"] > 1
    assert await connector.count(store) == first["chunks"]
    second = await connector.add_documents(store, [{"id": "handbook", "text": "Short replacement."}])
    assert second["chunks"] == 1
    assert await connector.count(store) == 1
    hits = await connector.search(store, "replacement")
    assert hits[0]["text"] == "Short replacement."


async def test_backend_refuses_mixed_embedding_spaces(backend):
    connector, registry, make_store = backend
    await connector.add_documents(registry.vector_store("s"), DOCS[:1])
    other = ConnectionRegistry(vector_stores={"s": make_store(provider="scripted", model="hashing-64",
                                                              params={"dimensions": 64})})  # fmt: skip
    mixed = VectorStoreConnector(other, EmbeddingRegistry(other))
    mixed.memory = connector.memory  # same process-local data for the memory backend
    mixed._internal_pool = connector._internal_pool
    with pytest.raises(VectorStoreError, match="re-ingest into a new collection"):
        await mixed.search(other.vector_store("s"), "vpn")
    with pytest.raises(VectorStoreError, match="re-ingest into a new collection"):
        await mixed.add_documents(other.vector_store("s"), DOCS[1:2])
    await mixed.aclose()


async def test_internal_pgvector_without_database_is_a_clear_error():
    registry = ConnectionRegistry(
        vector_stores={
            "docs": VectorStoreSpec(provider="pgvector", collection="c", embedding=EmbeddingSpec(**SCRIPTED))
        }
    )
    connector = VectorStoreConnector(registry, EmbeddingRegistry(registry))
    with pytest.raises(VectorStoreError, match="needs MAW_DATABASE_URL"):
        await connector.search(registry.vector_store("docs"), "anything")


# --------------------------------------------------------------------------------------------------
# The rag node and the retriever tool (memory store via the full service)
# --------------------------------------------------------------------------------------------------


def _memory_connections(tmp_path: Path, collection: str = "docs") -> Path:
    path = tmp_path / "rag-connections.yaml"
    path.write_text(
        "vector_stores:\n  docs:\n    provider: memory\n"
        f"    collection: {collection}\n    description: Company docs.\n"
        "    embedding: {provider: scripted, model: hashing-256}\n"
    )
    return path


@pytest.fixture
async def rag_service(tmp_path):
    async with AsyncExitStack() as stack:
        service = await build_service(
            make_settings(tmp_path, workflows_dir=None, connections_file=_memory_connections(tmp_path)), stack
        )
        store = service.ctx.connections.vector_store("docs")
        await service.ctx.vectors.add_documents(
            store,
            [
                {"id": p.name, "text": p.read_text(), "metadata": {"source": p.name}}
                for p in sorted(DOCS_DIR.glob("*.md"))
            ],
        )
        yield service


async def run(service, raw, input=""):
    definition, _ = service.resolve(workflow=raw)
    return await service.start(definition, input)


async def test_rag_node_feeds_context_to_the_next_node(rag_service):
    raw = wf(
        [
            {"id": "retrieve", "type": "rag", "store": "docs", "top_k": 2, "write_output": False},
            {
                "id": "answer",
                "type": "llm",
                "model": "scripted",
                "user_message": "Context:\n{{ nodes.retrieve.context }}\n\nQuestion: {{ input }}",
            },
        ],  # fmt: skip
        chain("retrieve", "answer"),
    )
    record = await run(rag_service, raw, "how many days of annual leave do I get")
    assert record.status == "completed", record.error
    assert record.trace[0]["store"] == "docs" and record.trace[0]["hits"] == 2
    assert "[1] (annual-leave.md)" in record.text and "24 days" in record.text  # the echo model saw it


async def test_rag_example_workflow(rag_service):
    raw = json.loads((REPO_ROOT / "examples" / "rag_qa.json").read_text())
    for question, source in [
        ("my vpn password expired, how do I reset it?", "vpn-password.md"),
        ("how long until a duplicate charge is refunded", "invoice-refunds.md"),
        ("where do I submit expense receipts", "expense-claims.md"),
    ]:
        record = await run(rag_service, raw, question)
        assert record.status == "completed", record.error
        assert record.output["source"] == source


async def test_rag_query_template_filter_and_empty_query(rag_service):
    raw = wf(
        [
            {
                "id": "r",
                "type": "rag",
                "store": "docs",
                "query": "{{ input.q }}",
                "filter": {"source": "input.only"},
                "top_k": 5,
            }
        ],  # fmt: skip
        chain("r"),
    )
    record = await run(rag_service, raw, {"q": "password", "only": "annual-leave.md"})
    assert record.status == "completed", record.error
    assert {d["metadata"]["source"] for d in record.output["documents"]} == {"annual-leave.md"}
    empty = await run(rag_service, raw, {"q": "   "})
    assert empty.output == {"query": "", "documents": [], "count": 0, "context": ""}


async def test_llm_searches_a_store_with_the_retriever_tool(rag_service):
    raw = wf(
        [{"id": "agent", "type": "llm", "model": "m", "tools": {"retrievers": ["docs"]}}],
        chain("agent"),
        models={
            "m": {
                "provider": "scripted",
                "model": "m",
                "params": {
                    "responses": [
                        {
                            "tool_calls": [
                                {"name": "search_docs", "args": {"query": "laptop will not charge"}}
                            ]
                        },
                        "Try another outlet, then hold the power button for 30 seconds.",
                    ]
                },
            }
        },  # fmt: skip
    )
    record = await run(rag_service, raw, "my laptop won't charge")
    assert record.status == "completed", record.error
    assert record.trace[0]["tool_calls"] == 1
    tool = __import__("maw.engine.nodes.rag", fromlist=["x"]).retriever_tool(rag_service.ctx, "docs", None)
    assert tool.name == "search_docs" and "Company docs." in tool.description
    passages = await tool.ainvoke({"query": "laptop will not charge"})
    assert passages.startswith("[1] (laptop-charging.md)")


async def test_rag_on_internal_pgvector_through_the_service(tmp_path, database_url):
    path = tmp_path / "pg-connections.yaml"
    collection = f"svc_{uuid.uuid4().hex[:8]}"
    path.write_text(
        f"vector_stores:\n  docs:\n    provider: pgvector\n    collection: {collection}\n"
        "    embedding: {provider: scripted, model: hashing-256}\n"
    )
    async with AsyncExitStack() as stack:
        service = await build_service(
            make_settings(tmp_path, workflows_dir=None, connections_file=path, database_url=database_url),
            stack,
        )
        store = service.ctx.connections.vector_store("docs")
        try:
            await service.ctx.vectors.add_documents(store, DOCS)
        except VectorStoreError as e:
            pytest.skip(f"pgvector unavailable: {e}")
        raw = wf([{"id": "r", "type": "rag", "store": "docs", "top_k": 1}], chain("r"))
        record = await run(service, raw, "annual leave days")
        assert record.status == "completed", record.error
        assert record.output["documents"][0]["document_id"] == "leave"


async def test_rag_failure_names_the_node(tmp_path):
    path = tmp_path / "pg-only.yaml"
    path.write_text("vector_stores:\n  docs:\n    provider: pgvector\n    collection: c\n"
                    "    embedding: {provider: scripted, model: hashing-256}\n")  # fmt: skip
    async with AsyncExitStack() as stack:
        service = await build_service(
            make_settings(tmp_path, workflows_dir=None, connections_file=path), stack
        )
        record = await run(service, wf([{"id": "r", "type": "rag", "store": "docs"}], chain("r")), "x")
    assert record.status == "failed"
    assert record.error["node"] == "r" and "MAW_DATABASE_URL" in record.error["message"]


# --------------------------------------------------------------------------------------------------
# HTTP API and CLI
# --------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rag_api(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rag-api")
    port = free_port()
    settings = make_settings(
        tmp, connections_file=_memory_connections(tmp), port=port, public_url=f"http://127.0.0.1:{port}"
    )
    with ServerThread(settings) as server:
        yield server.url


async def test_vector_store_api(rag_api):
    async with httpx.AsyncClient(base_url=rag_api, timeout=30) as http:
        listed = (await http.get("/v1/vector-stores")).json()
        assert (
            listed["docs"]["provider"] == "memory" and listed["docs"]["embedding"] == "scripted:hashing-256"
        )
        docs = [{"id": f"docs/{p.name}", "text": p.read_text(), "metadata": {"source": p.name}}
                for p in sorted(DOCS_DIR.glob("*.md"))]  # fmt: skip
        ingested = (await http.post("/v1/vector-stores/docs/documents", json={"documents": docs})).json()
        assert ingested["documents"] == 5
        assert (await http.get("/v1/vector-stores/docs")).json()["chunks"] == ingested["chunks"]

        found = (
            await http.post("/v1/vector-stores/docs/query", json={"query": "expense receipts", "top_k": 1})
        ).json()
        assert found["documents"][0]["metadata"]["source"] == "expense-claims.md"
        assert found["context"].startswith("[1] (expense-claims.md)")

        run = (await http.post("/v1/runs", json={"workflow": json.loads((REPO_ROOT / "examples" / "rag_qa.json").read_text()),
                                                 "input": "how many days of annual leave"})).json()  # fmt: skip
        assert run["status"] == "completed" and run["output"]["source"] == "annual-leave.md"

        deleted = await http.delete(
            "/v1/vector-stores/docs/documents/docs/annual-leave.md"
        )  # id with a slash
        assert deleted.status_code == 200 and deleted.json()["deleted_chunks"] >= 1
        assert (await http.delete("/v1/vector-stores/docs/documents/docs/annual-leave.md")).status_code == 404

        assert (await http.get("/v1/vector-stores/nope")).status_code == 404
        bad = await http.post("/v1/vector-stores/docs/documents", json={"documents": [{"text": "x"}], "chunk_size": 100,
                                                                       "chunk_overlap": 100})  # fmt: skip
        assert bad.status_code == 422
        assert (
            await http.post("/v1/vector-stores/docs/documents", json={"documents": []})
        ).status_code == 422
        assert "vector_stores" in (await http.get("/v1/connections")).json()


def test_cli_ingest(tmp_path):
    env = {
        **os.environ,
        "MAW_CONNECTIONS_FILE": str(_memory_connections(tmp_path)),
        "MAW_LOG_LEVEL": "WARNING",
    }
    env.pop("MAW_DATABASE_URL", None)
    files = [str(p) for p in sorted(DOCS_DIR.glob("*.md"))]
    ok = subprocess.run([sys.executable, "-m", "maw.main", "ingest", "docs", *files], env=env, cwd=REPO_ROOT,
                        capture_output=True, text=True, timeout=60)  # fmt: skip
    assert ok.returncode == 0, ok.stderr
    assert "ingested 5 documents" in ok.stdout
    unknown = subprocess.run([sys.executable, "-m", "maw.main", "ingest", "nope", files[0]], env=env, cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)  # fmt: skip
    assert unknown.returncode == 1 and "unknown vector store 'nope'" in unknown.stderr
    bad_file = subprocess.run([sys.executable, "-m", "maw.main", "ingest", "docs", "pyproject.toml"], env=env,
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)  # fmt: skip
    assert bad_file.returncode == 1 and "not a readable" in bad_file.stderr
