"""Open the run store and the LangGraph checkpointer (in-memory, or Postgres when configured)."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from .store import MemoryStore, PostgresStore, Store

log = logging.getLogger("maw.runtime")


@dataclass
class Persistence:
    store: Store
    checkpointer: Any
    durable: bool
    pool: Any = None  # the Postgres pool, shared with the internal pgvector store


async def open_persistence(database_url: str | None, stack: AsyncExitStack) -> Persistence:
    if not database_url:
        log.warning("MAW_DATABASE_URL not set: runs and checkpoints are in-memory and lost on restart")
        store = MemoryStore()
        await store.setup()
        return Persistence(store=store, checkpointer=InMemorySaver(), durable=False)

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=20,
        # autocommit + no prepared statements: required by the checkpointer and safe behind poolers.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True, timeout=30)
    stack.push_async_callback(pool.close)
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    store = PostgresStore(pool)
    await store.setup()
    log.info("durable persistence enabled (Postgres)")
    return Persistence(store=store, checkpointer=checkpointer, durable=True, pool=pool)
