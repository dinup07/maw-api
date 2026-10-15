"""Mock A2A 1.0 agent that answers questions about invoices, charges and refunds.

Dev/test only: shipped in the image for Docker Compose and used by the test suite, never imported
by ``src/``. Routes are mounted at the root (card at ``/.well-known/agent-card.json``).

    python mocks/a2a_billing_agent.py --port 9001 --public-url http://localhost:9001
"""

from __future__ import annotations

import argparse
import re
from typing import Any

import uvicorn
from a2a.helpers import new_data_part, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import TransportProtocol
from fastapi import FastAPI
from starlette.routing import Mount

INVOICES: dict[str, dict[str, Any]] = {
    "INV-1001": {"amount": 129.0, "status": "paid", "duplicate_charge": True},
    "INV-1002": {"amount": 59.5, "status": "open", "duplicate_charge": False},
}
_INVOICE = re.compile(r"INV-\d+", re.IGNORECASE)


def answer(text: str) -> tuple[str, dict[str, Any]]:
    """Return ``(reply_text, data)`` for a user message."""
    match = _INVOICE.search(text or "")
    if not match:
        return (
            "I can help with invoices. Please include an invoice number like INV-1001.",
            {"needs": "invoice_number"},
        )
    invoice = match.group(0).upper()
    record = INVOICES.get(invoice)
    if record is None:
        return f"I couldn't find {invoice}.", {"invoice": invoice, "found": False}
    if record["duplicate_charge"]:
        return (
            f"{invoice} was charged twice. A refund of ${record['amount']:.2f} has been issued "
            "and will arrive in 3 business days.",
            {"invoice": invoice, "found": True, "action": "refund_issued", "amount": record["amount"]},
        )
    return (
        f"{invoice} is {record['status']} for ${record['amount']:.2f}. No duplicate charge found.",
        {"invoice": invoice, "found": True, "action": "none", "status": record["status"]},
    )


class BillingExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: Any) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work(
            new_text_message("Looking up billing records...", context_id=task.context_id, task_id=task.id)
        )
        text, data = answer(context.get_user_input())
        await updater.add_artifact([new_text_part(text), new_data_part(data)], name="billing_answer")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: Any) -> None:
        task = context.current_task
        if task is not None:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()


def build_card(public_url: str) -> AgentCard:
    url = public_url.rstrip("/")
    return AgentCard(
        name="billing-agent",
        description="Answers questions about invoices, charges and refunds.",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding=TransportProtocol.JSONRPC, protocol_version="1.0"),
            AgentInterface(url=url, protocol_binding=TransportProtocol.HTTP_JSON, protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="invoices",
                name="Invoices",
                description="Look up invoices, duplicate charges and refunds.",
                tags=["billing", "invoices", "refunds"],
                examples=["Was INV-1001 charged twice?"],
            )
        ],
    )


def build_app(public_url: str) -> FastAPI:
    card = build_card(public_url)
    handler = DefaultRequestHandler(
        agent_executor=BillingExecutor(), task_store=InMemoryTaskStore(), agent_card=card
    )
    app = FastAPI(title="Mock billing agent")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
        # The SDK adds a catch-all Mount('/{tenant}'); drop it (single-tenant).
        rest_routes=[r for r in create_rest_routes(handler) if not isinstance(r, Mount)],
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--public-url", default=None)
    args = parser.parse_args()
    public_url = args.public_url or f"http://localhost:{args.port}"
    uvicorn.run(build_app(public_url), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
