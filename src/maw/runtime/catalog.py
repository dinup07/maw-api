"""The workflow catalog: file-defined workflows plus workflows registered through the API."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..schema import WorkflowDefinition, WorkflowValidationError, parse_workflow
from .store import Store, StoredWorkflow, utcnow

log = logging.getLogger("maw.catalog")


@dataclass
class CatalogEntry:
    workflow: WorkflowDefinition
    source: Literal["file", "api"]
    hash: str
    warnings: list[str]


def _default_id(raw: dict[str, Any], stem: str) -> dict[str, Any]:
    target = raw["workflow_config"] if isinstance(raw.get("workflow_config"), dict) else raw
    target.setdefault("id", stem)
    return raw


def _read_dir(directory: Path) -> list[tuple[Path, Any]]:
    out: list[tuple[Path, Any]] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.rglob("*.json")):
        try:
            out.append((path, json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "skipping unreadable workflow file", extra={"data": {"file": str(path), "error": str(e)}}
            )
    return out


class Catalog:
    def __init__(self, store: Store, validator: Callable[[WorkflowDefinition], None]):
        self.store = store
        self.validator = validator
        self._entries: dict[str, CatalogEntry] = {}

    def _parse(self, raw: Any) -> tuple[WorkflowDefinition, list[str]]:
        wf, warnings = parse_workflow(raw)
        self.validator(wf)
        return wf, warnings

    async def load(self, directory: Path | str | None) -> None:
        if directory is not None:
            for path, raw in await asyncio.to_thread(_read_dir, Path(directory)):
                if not isinstance(raw, dict) or not ("nodes" in raw or "workflow_config" in raw):
                    continue  # not a workflow
                try:
                    wf, warnings = self._parse(_default_id(raw, path.stem))
                except WorkflowValidationError as e:
                    log.error(
                        "skipping invalid workflow", extra={"data": {"file": str(path), "errors": e.errors}}
                    )
                    continue
                if wf.id in self._entries:
                    log.error(
                        "skipping duplicate workflow id", extra={"data": {"file": str(path), "id": wf.id}}
                    )
                    continue
                self._entries[wf.id] = CatalogEntry(wf, "file", wf.content_hash(), warnings)

        for stored in await self.store.list_workflows():
            if stored.id in self._entries:
                log.warning("API workflow shadowed by a file workflow", extra={"data": {"id": stored.id}})
                continue
            try:
                wf, warnings = self._parse(stored.definition)
            except WorkflowValidationError as e:
                log.error(
                    "skipping invalid stored workflow", extra={"data": {"id": stored.id, "errors": e.errors}}
                )
                continue
            self._entries[wf.id] = CatalogEntry(wf, "api", wf.content_hash(), warnings)
        log.info("catalog loaded", extra={"data": {"workflows": sorted(self._entries)}})

    async def register(self, raw: Any) -> CatalogEntry:
        wf, warnings = self._parse(raw)
        existing = self._entries.get(wf.id)
        if existing is not None and existing.source == "file":
            raise WorkflowValidationError([f"workflow '{wf.id}' is defined in a file and can't be replaced"])
        entry = CatalogEntry(wf, "api", wf.content_hash(), warnings)
        await self.store.save_workflow(
            StoredWorkflow(
                id=wf.id,
                hash=entry.hash,
                definition=wf.model_dump(mode="json"),
                source="api",
                updated_at=utcnow(),
            )
        )
        self._entries[wf.id] = entry
        return entry

    async def delete(self, workflow_id: str) -> bool:
        entry = self._entries.get(workflow_id)
        if entry is None:
            return False
        if entry.source == "file":
            raise WorkflowValidationError(
                [f"workflow '{workflow_id}' is defined in a file and can't be deleted"]
            )
        await self.store.delete_workflow(workflow_id)
        del self._entries[workflow_id]
        return True

    def get(self, workflow_id: str) -> CatalogEntry | None:
        return self._entries.get(workflow_id)

    def entries(self) -> list[CatalogEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def exposed(self) -> list[CatalogEntry]:
        return [e for e in self.entries() if e.workflow.expose.a2a]

    def __len__(self) -> int:
        return len(self._entries)
