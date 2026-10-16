"""FastAPI application factory, lifespan and the engine wiring shared with the tests."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from .. import __version__
from ..a2a_server import mount_workflow_agent
from ..connectors import (
    A2AConnector,
    ConnectionRegistry,
    EmbeddingRegistry,
    McpConnector,
    VectorStoreConnector,
)
from ..engine.models import ModelRegistry
from ..engine.nodes import EngineContext
from ..functions import load_function_modules
from ..observability import setup_logging, setup_tracing
from ..runtime import Catalog, RunService, open_persistence
from ..schema import WorkflowDefinition
from ..settings import Settings, get_settings
from .auth import ApiKeyMiddleware
from .errors import install_error_handlers
from .routers import connections, health, runs, vector_stores, workflows

log = logging.getLogger("maw.api")


def build_engine_context(settings: Settings) -> EngineContext:
    """Connections from the trusted file, the model registry and both outbound connectors."""
    connections_ = ConnectionRegistry.from_file(settings.connections_file, settings.allowed_outbound_hosts)
    return EngineContext(
        connections=connections_,
        models=ModelRegistry(connections_, allow_inline_api_keys=settings.allow_inline_api_keys),
        mcp=McpConnector(
            tool_cache_ttl_s=settings.mcp_tool_cache_ttl_s, default_timeout_s=settings.outbound_timeout_s
        ),
        a2a=A2AConnector(
            connections_,
            card_ttl_s=settings.a2a_card_cache_ttl_s,
            default_timeout_s=settings.outbound_timeout_s,
        ),
        vectors=VectorStoreConnector(
            connections_,
            EmbeddingRegistry(connections_, allow_inline_api_keys=settings.allow_inline_api_keys),
            default_timeout_s=settings.outbound_timeout_s,
        ),
        default_max_tool_calls=settings.default_max_tool_calls,
        default_max_node_visits=settings.default_max_node_visits,
    )


def mount_agent(app: FastAPI, service: RunService, wf: WorkflowDefinition) -> bool:
    """Mount a workflow's A2A routes once. Returns False if it was already mounted."""
    mounted: set[str] = app.state.mounted_agents
    if wf.id in mounted:
        return False
    mount_workflow_agent(app, service, wf, service.settings.public_url)
    mounted.add(wf.id)
    app.openapi_schema = None  # so /docs picks up the new routes
    return True


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level, settings.log_json)
        if settings.tracing_enabled:
            setup_tracing(settings.service_name)
        load_function_modules(settings.function_modules)
        async with AsyncExitStack() as stack:
            ctx = build_engine_context(settings)
            stack.push_async_callback(ctx.a2a.aclose)
            stack.push_async_callback(ctx.vectors.aclose)
            persistence = await open_persistence(settings.database_url, stack)
            if persistence.pool is not None:
                ctx.vectors.attach_pool(persistence.pool)  # the internal pgvector store
            service: RunService | None = None
            # Late-bound: the catalog validates with the service's known connections and functions.
            catalog = Catalog(persistence.store, validator=lambda wf: service.validate(wf))
            service = RunService(settings, ctx, persistence, catalog)
            await catalog.load(settings.workflows_dir)
            app.state.service = service
            app.state.mounted_agents = set()
            for entry in catalog.exposed():
                mount_agent(app, service, entry.workflow)
            try:
                recovered = await service.recover()
                log.info(
                    "MAW ready",
                    extra={
                        "data": {
                            "version": __version__,
                            "recovered_runs": recovered,
                            "durable": service.durable,
                        }
                    },
                )
                yield
            finally:
                await service.shutdown()

    app = FastAPI(title="MAW — Multi-Agent Workflow runtime", version=__version__, lifespan=lifespan)
    install_error_handlers(app)
    for router in (health.router, runs.router, workflows.router, connections.router, vector_stores.router):
        app.include_router(router)
    if settings.api_keys:
        app.add_middleware(ApiKeyMiddleware, api_keys=settings.api_keys)
    return app
