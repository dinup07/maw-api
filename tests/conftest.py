"""Shared fixtures and helpers. Grows with each build phase (mocks, services, API server)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
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

import httpx  # noqa: E402
import pytest  # noqa: E402

from maw.api.app import build_engine_context  # noqa: E402
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


async def build_service(settings: Settings, stack: AsyncExitStack) -> RunService:
    load_function_modules(settings.function_modules)
    ctx = build_engine_context(settings)
    stack.push_async_callback(ctx.vectors.aclose)
    persistence = await open_persistence(settings.database_url, stack)
    if persistence.pool is not None:
        ctx.vectors.attach_pool(persistence.pool)
    service: RunService | None = None
    catalog = Catalog(persistence.store, validator=lambda definition: service.validate(definition))
    service = RunService(settings, ctx, persistence, catalog)
    await catalog.load(settings.workflows_dir)
    stack.push_async_callback(ctx.a2a.aclose)
    stack.push_async_callback(service.shutdown)
    return service


@pytest.fixture
async def service(tmp_path):
    async with AsyncExitStack() as stack:
        yield await build_service(make_settings(tmp_path, workflows_dir=None), stack)


@pytest.fixture
async def live_service(tmp_path, connections_file):
    async with AsyncExitStack() as stack:
        yield await build_service(make_settings(tmp_path, connections_file=connections_file), stack)


@pytest.fixture
def database_url():
    url = os.environ.get("MAW_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MAW_TEST_DATABASE_URL is not set")
    return url


# --------------------------------------------------------------------------------------------------
# Mock services (real subprocesses)
# --------------------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _spawn(args: list[str], probe_url: str, timeout: float = 30) -> subprocess.Popen:
    """Start a mock from the repo root and wait until it answers (any of 200/404/405/406)."""
    proc = subprocess.Popen(
        [sys.executable, *args], cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{args[0]} exited early: {proc.stderr.read().decode()[-2000:]}")
        try:
            if httpx.get(probe_url, timeout=1).status_code in (200, 404, 405, 406):
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"{args[0]} did not start within {timeout}s")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def mcp_url():
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    proc = _spawn(["mocks/mcp_kb_server.py", "--host", "127.0.0.1", "--port", str(port)], url)
    yield url
    _stop(proc)


@pytest.fixture(scope="session")
def billing_url():
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    proc = _spawn(
        ["mocks/a2a_billing_agent.py", "--host", "127.0.0.1", "--port", str(port), "--public-url", url],
        f"{url}/.well-known/agent-card.json",
    )
    yield url
    _stop(proc)


@pytest.fixture
def connections_file(tmp_path, mcp_url, billing_url) -> Path:
    path = tmp_path / "connections.yaml"
    path.write_text(
        f"mcp_servers:\n  kb:\n    url: {mcp_url}\nagents:\n  billing:\n    url: {billing_url}\n"
        "    description: Answers questions about invoices, charges and refunds.\n"
    )
    return path


# --------------------------------------------------------------------------------------------------
# A real API server in a background thread
# --------------------------------------------------------------------------------------------------

import threading  # noqa: E402

import uvicorn  # noqa: E402

from maw.api.app import create_app  # noqa: E402


class ServerThread:
    def __init__(self, settings):
        self.app = create_app(settings)
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=settings.port, log_config=None, lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://127.0.0.1:{settings.port}"

    def __enter__(self) -> ServerThread:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("API server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
