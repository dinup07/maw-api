"""API-key authentication as pure ASGI middleware.

Not ``BaseHTTPMiddleware``: that buffers responses and would break Server-Sent Events.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

PUBLIC_PATHS = frozenset(
    {"/health", "/ready", "/metrics", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)
AGENT_CARD_SUFFIX = "/.well-known/agent-card.json"


class ApiKeyMiddleware:
    def __init__(self, app: Any, api_keys: list[str]):
        self.app = app
        self._keys = [k.encode() for k in api_keys if k]

    def _authorized(self, headers: list[tuple[bytes, bytes]]) -> bool:
        presented: list[bytes] = []
        for name, value in headers:
            lname = name.lower()
            if lname == b"x-api-key":
                presented.append(value.strip())
            elif lname == b"authorization" and value[:7].lower() == b"bearer ":
                presented.append(value[7:].strip())
        # Compare against every key so timing doesn't reveal which (if any) matched.
        matched = False
        for candidate in presented:
            for key in self._keys:
                matched |= hmac.compare_digest(candidate, key)
        return matched

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if (
            path in PUBLIC_PATHS
            or path.endswith(AGENT_CARD_SUFFIX)
            or self._authorized(scope.get("headers", []))
        ):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"detail": "missing or invalid API key"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
