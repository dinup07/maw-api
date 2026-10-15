"""Shared fixtures and helpers. Grows with each build phase (mocks, services, API server)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).parent
REPO_ROOT = TESTS_DIR.parent
WORKFLOWS_DIR = REPO_ROOT / "workflows"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from maw.functions import load_function_modules  # noqa: E402

load_function_modules(["fixture_functions"])


def wf(nodes: list[dict], edges: list[dict], **extra: Any) -> dict[str, Any]:
    """Build a raw workflow definition."""
    return {"id": extra.pop("id", "test"), "nodes": nodes, "edges": edges, **extra}


def chain(*ids: str) -> list[dict]:
    """Linear edges start -> ids... -> end."""
    path = ["start", *ids, "end"]
    return [{"source": a, "target": b} for a, b in zip(path, path[1:], strict=False)]


# --------------------------------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------------------------------

from contextlib import AsyncExitStack  # noqa: E402

import pytest  # noqa: E402

from maw.connectors import ConnectionRegistry  # noqa: E402
from maw.engine.models import ModelRegistry  # noqa: E402
from maw.engine.nodes import EngineContext  # noqa: E402
from maw.runtime import Catalog, RunService, open_persistence  # noqa: E402
from maw.settings import Settings  # noqa: E402


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "workflows_dir": WORKFLOWS_DIR,
        "connections_file": tmp_path / "no-connections.yaml",
        "database_url": None,
        "log_json": False,
        "public_url": "http://127.0.0.1:8000",
    }
    values.update(overrides)
    return Settings(**values)


def build_engine_context(settings: Settings) -> EngineContext:
    # Replaced by maw.api.app.build_engine_context once the API layer lands.
    connections = ConnectionRegistry.from_file(settings.connections_file, settings.allowed_outbound_hosts)
    return EngineContext(
        connections=connections,
        models=ModelRegistry(connections, allow_inline_api_keys=settings.allow_inline_api_keys),
        default_max_tool_calls=settings.default_max_tool_calls,
        default_max_node_visits=settings.default_max_node_visits,
    )


async def build_service(settings: Settings, stack: AsyncExitStack) -> RunService:
    load_function_modules(settings.function_modules)
    ctx = build_engine_context(settings)
    persistence = await open_persistence(settings.database_url, stack)
    service: RunService | None = None
    catalog = Catalog(persistence.store, validator=lambda definition: service.validate(definition))
    service = RunService(settings, ctx, persistence, catalog)
    await catalog.load(settings.workflows_dir)
    stack.push_async_callback(service.shutdown)
    return service


@pytest.fixture
async def service(tmp_path):
    async with AsyncExitStack() as stack:
        yield await build_service(make_settings(tmp_path, workflows_dir=None), stack)
