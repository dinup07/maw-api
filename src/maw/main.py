"""Command-line interface: ``maw serve | validate | schema``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .connectors import ConnectionRegistry
from .functions import function_names, load_function_modules
from .schema import WorkflowDefinition, WorkflowValidationError, parse_workflow, validate_graph
from .settings import get_settings


def _with_default_id(raw: Any, stem: str) -> Any:
    """Default the workflow id to the filename stem (inside ``workflow_config`` if present)."""
    if isinstance(raw, dict):
        target = raw.get("workflow_config") if isinstance(raw.get("workflow_config"), dict) else raw
        target.setdefault("id", stem)
    return raw


def cmd_validate(files: list[str]) -> int:
    settings = get_settings()
    load_function_modules(settings.function_modules)
    registry = ConnectionRegistry.from_file(settings.connections_file, settings.allowed_outbound_hosts)
    mcp_servers, agents = list(registry.mcp_servers), list(registry.agents)
    vector_stores = list(registry.vector_stores)
    functions = function_names()
    failed = 0
    for name in files:
        path = Path(name)
        try:
            raw = _with_default_id(json.loads(path.read_text()), path.stem)
            wf, warnings = parse_workflow(raw)
            validate_graph(
                wf,
                known_mcp_servers=mcp_servers,
                known_agents=agents,
                known_functions=functions,
                known_vector_stores=vector_stores,
            )
        except (OSError, json.JSONDecodeError) as e:
            failed += 1
            print(f"FAIL  {name}\n      - {e}")
            continue
        except WorkflowValidationError as e:
            failed += 1
            print(f"FAIL  {name}")
            for err in e.errors:
                print(f"      - {err}")
            continue
        print(f"ok    {name}  ({len(wf.nodes)} nodes, hash {wf.content_hash()[:12]})")
        for warning in warnings:
            print(f"      warning: {warning}")
    return 1 if failed else 0


INGEST_SUFFIXES = (".txt", ".md", ".markdown", ".rst")


async def _ingest(store_name: str, paths: list[Path], chunk_size: int, chunk_overlap: int) -> int:
    from contextlib import AsyncExitStack

    from .api.app import build_engine_context
    from .runtime import open_persistence

    settings = get_settings()
    ctx = build_engine_context(settings)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(ctx.vectors.aclose)
        stack.push_async_callback(ctx.a2a.aclose)
        if store_name not in ctx.connections.vector_stores:
            known = ", ".join(sorted(ctx.connections.vector_stores)) or "none"
            print(f"unknown vector store '{store_name}' (configured: {known})", file=sys.stderr)
            return 1
        store = ctx.connections.vector_store(store_name)
        if store.spec.provider == "pgvector" and not store.spec.url:
            persistence = await open_persistence(settings.database_url, stack)
            if persistence.pool is not None:
                ctx.vectors.attach_pool(persistence.pool)
        documents = [
            {
                "id": path.as_posix(),
                "text": path.read_text(encoding="utf-8"),
                "metadata": {"source": path.name},
            }
            for path in paths
        ]
        result = await ctx.vectors.add_documents(
            store, documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        total = await ctx.vectors.count(store)
    print(
        f"ingested {result['documents']} documents ({result['chunks']} chunks) into '{store_name}'; {total} chunks total"
    )
    return 0


def cmd_ingest(store: str, files: list[str], chunk_size: int, chunk_overlap: int) -> int:
    import asyncio

    from .connectors import ConnectionError_, VectorStoreError

    paths = [Path(f) for f in files]
    bad = [p for p in paths if not p.is_file() or p.suffix.lower() not in INGEST_SUFFIXES]
    if bad:
        print(f"not a readable {'/'.join(INGEST_SUFFIXES)} file: {', '.join(map(str, bad))}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_ingest(store, paths, chunk_size, chunk_overlap))
    except (ConnectionError_, VectorStoreError, ValueError) as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return 1


def cmd_schema() -> int:
    print(json.dumps(WorkflowDefinition.model_json_schema(), indent=2))
    return 0


def cmd_serve(host: str | None, port: int | None, reload: bool) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "maw.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_config=None,
        proxy_headers=True,
    )
    return 0


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="maw", description="MAW - Multi-Agent Workflow runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true")

    validate = sub.add_parser("validate", help="validate workflow JSON files")
    validate.add_argument("files", nargs="+", metavar="FILE")

    sub.add_parser("schema", help="print the workflow JSON Schema")

    ingest = sub.add_parser("ingest", help="chunk, embed and store text files in a vector store")
    ingest.add_argument("store", help="vector store name from the connections file")
    ingest.add_argument("files", nargs="+", metavar="FILE")
    ingest.add_argument("--chunk-size", type=int, default=1000)
    ingest.add_argument("--chunk-overlap", type=int, default=150)

    args = parser.parse_args(argv)
    if args.command == "validate":
        code = cmd_validate(args.files)
    elif args.command == "schema":
        code = cmd_schema()
    elif args.command == "ingest":
        code = cmd_ingest(args.store, args.files, args.chunk_size, args.chunk_overlap)
    else:
        code = cmd_serve(args.host, args.port, args.reload)
    sys.exit(code)


if __name__ == "__main__":
    cli()
