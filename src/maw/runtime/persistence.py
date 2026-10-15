"""Open the run store and the LangGraph checkpointer (in-memory, or Postgres when configured)."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from .store import MemoryStore, Store

log = logging.getLogger("maw.runtime")


@dataclass
class Persistence:
    store: Store
    checkpointer: Any
    durable: bool


async def open_persistence(database_url: str | None, stack: AsyncExitStack) -> Persistence:
    if not database_url:
        log.warning("MAW_DATABASE_URL not set: runs and checkpoints are in-memory and lost on restart")
        store = MemoryStore()
        await store.setup()
        return Persistence(store=store, checkpointer=InMemorySaver(), durable=False)
    raise NotImplementedError("Postgres persistence is added in the durability phase")
