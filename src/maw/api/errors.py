"""Map domain exceptions to HTTP responses.

Failures *during* a run are not HTTP errors: the run is recorded as ``failed`` and returned with 200.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..connectors import A2ACallError, ConnectionError_, McpToolError
from ..engine.models import ModelConfigError
from ..runtime import RunNotFound, RunStateError
from ..schema import WorkflowValidationError


async def _validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, WorkflowValidationError)
    return JSONResponse(status_code=422, content={"detail": "invalid workflow", "errors": exc.errors})


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc.args[0]) if exc.args else "not found"})


async def _conflict(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


async def _bad_request(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def _bad_gateway(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(WorkflowValidationError, _validation)
    app.add_exception_handler(RunNotFound, _not_found)
    app.add_exception_handler(RunStateError, _conflict)
    app.add_exception_handler(ConnectionError_, _bad_request)
    app.add_exception_handler(ModelConfigError, _bad_request)
    # Only reachable from the connection-discovery endpoints; inside a run these fail the run.
    app.add_exception_handler(McpToolError, _bad_gateway)
    app.add_exception_handler(A2ACallError, _bad_gateway)
