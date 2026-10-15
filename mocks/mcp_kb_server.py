"""Mock MCP server: a tiny IT/HR/billing knowledge base with ticketing.

Dev/test only: shipped in the image for Docker Compose and used by the test suite, never imported
by ``src/``. Serves streamable HTTP at ``/mcp``.

    python mocks/mcp_kb_server.py --host 0.0.0.0 --port 8001
"""

from __future__ import annotations

import argparse
import itertools
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

ARTICLES = [
    {
        "id": "KB-101",
        "title": "Reset your VPN password",
        "tags": ["vpn", "password", "it"],
        "body": "Open the self-service portal, choose 'VPN', then 'Reset password'. Changes apply in 5 minutes.",
    },
    {
        "id": "KB-102",
        "title": "Laptop will not charge",
        "tags": ["hardware", "laptop", "it"],
        "body": "Try a different outlet, then hold the power button for 30 seconds. If it persists, open a ticket.",
    },
    {
        "id": "KB-201",
        "title": "Annual leave policy",
        "tags": ["leave", "hr", "policy"],
        "body": "Full-time staff accrue 24 days per year. Requests go through the HR portal two weeks in advance.",
    },
    {
        "id": "KB-301",
        "title": "Duplicate invoice charges",
        "tags": ["invoice", "billing", "refund"],
        "body": "Duplicate charges are refunded automatically within 3 business days after verification.",
    },
]

_ticket_ids = itertools.count(1001)

mcp = FastMCP("support-kb")


@mcp.tool()
def search_kb(query: str, limit: int = 3) -> dict[str, Any]:
    """Search the knowledge base. Returns the best-matching articles."""
    words = [w for w in re.sub(r"[^\w\s]", " ", query.lower()).split() if len(w) > 2]
    scored = []
    for article in ARTICLES:
        haystack = " ".join([article["title"], article["body"], *article["tags"]]).lower()
        score = sum(haystack.count(w) for w in words)
        if score > 0:
            scored.append((score, article))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    articles = [{"id": a["id"], "title": a["title"], "body": a["body"]} for _, a in scored[: max(limit, 0)]]
    return {"query": query, "count": len(articles), "articles": articles}


@mcp.tool()
def create_ticket(title: str, priority: str = "normal", category: str = "general") -> dict[str, Any]:
    """Open a service-desk ticket. priority: low | normal | high."""
    if priority not in ("low", "normal", "high"):
        raise ValueError("priority must be low, normal or high")
    return {
        "ticket_id": f"TCK-{next(_ticket_ids)}",
        "title": title,
        "priority": priority,
        "category": category,
        "status": "open",
    }


@mcp.tool()
def get_ticket(ticket_id: str) -> dict[str, Any]:
    """Look up a ticket's status."""
    return {"ticket_id": ticket_id, "status": "in_progress", "assignee": "service-desk"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
