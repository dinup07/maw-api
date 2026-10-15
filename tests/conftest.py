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
