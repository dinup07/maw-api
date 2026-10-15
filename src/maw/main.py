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
    functions = function_names()
    failed = 0
    for name in files:
        path = Path(name)
        try:
            raw = _with_default_id(json.loads(path.read_text()), path.stem)
            wf, warnings = parse_workflow(raw)
            validate_graph(wf, known_mcp_servers=mcp_servers, known_agents=agents, known_functions=functions)
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

    args = parser.parse_args(argv)
    if args.command == "validate":
        code = cmd_validate(args.files)
    elif args.command == "schema":
        code = cmd_schema()
    else:
        code = cmd_serve(args.host, args.port, args.reload)
    sys.exit(code)


if __name__ == "__main__":
    cli()
