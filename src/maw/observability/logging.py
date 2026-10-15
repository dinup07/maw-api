"""Structured logging with run/workflow/node ids injected from contextvars."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

run_id_var: ContextVar[str | None] = ContextVar("maw_run_id", default=None)
workflow_id_var: ContextVar[str | None] = ContextVar("maw_workflow_id", default=None)
node_id_var: ContextVar[str | None] = ContextVar("maw_node_id", default=None)

_CONTEXT = (("run_id", run_id_var), ("workflow_id", workflow_id_var), ("node_id", node_id_var))
_QUIET = ("httpx", "httpcore", "mcp", "uvicorn.access")


@contextmanager
def bind(var: ContextVar, value: Any) -> Iterator[None]:
    token = var.set(value)
    try:
        yield
    finally:
        var.reset(token)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CONTEXT:
            if not hasattr(record, name):
                setattr(record, name, var.get())
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name, _ in _CONTEXT:
            value = getattr(record, name, None)
            if value:
                entry[name] = value
        data = getattr(record, "data", None)
        if data is not None:
            entry["data"] = data
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s%(ctx)s: %(message)s%(data_text)s")

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"{key.removesuffix('_id')}={getattr(record, key)}"
            for key in ("run_id", "node_id")
            if getattr(record, key, None)
        ]
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        data = getattr(record, "data", None)
        record.data_text = f" {json.dumps(data, default=str)}" if data is not None else ""
        return super().format(record)


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
