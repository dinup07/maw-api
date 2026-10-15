"""Named MCP servers and A2A agents, secret substitution and the outbound-host allowlist.

Trust model: the server-side connections file is *trusted* (operator-controlled); anything inside a
submitted workflow is *untrusted* (caller-controlled). Untrusted values may only read
``MAW_SECRET_*`` variables via ``${secret:NAME}`` — never arbitrary env vars — so a workflow can't
send, say, ``OPENAI_API_KEY`` to a server it controls.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import TypeAdapter

from ..schema.workflow import AgentSpec, McpServerSpec, WorkflowDefinition

_SECRET = re.compile(r"\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
SECRET_PREFIX = "MAW_SECRET_"


class ConnectionError_(RuntimeError):  # noqa: N801 - trailing underscore avoids shadowing the builtin
    """A connection is unknown, misconfigured or not allowed."""


@dataclass(frozen=True)
class Resolved[T]:
    name: str
    spec: T
    trusted: bool


def resolve_secrets(value: str, *, trusted: bool) -> str:
    def secret(match: re.Match) -> str:
        name = match.group(1)
        env_name = SECRET_PREFIX + name
        if env_name not in os.environ:
            raise ConnectionError_(f"secret '{name}' is not set (expected env var {env_name})")
        return os.environ[env_name]

    value = _SECRET.sub(secret, value)
    if not _ENV.search(value):
        return value
    if not trusted:
        raise ConnectionError_(
            "${env:...} is only allowed in the server-side connections file; use ${secret:...}"
        )

    def env(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConnectionError_(f"environment variable '{name}' is not set")

    return _ENV.sub(env, value)


def resolve_secrets_deep(value: Any, *, trusted: bool) -> Any:
    if isinstance(value, str):
        return resolve_secrets(value, trusted=trusted)
    if isinstance(value, dict):
        return {k: resolve_secrets_deep(v, trusted=trusted) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets_deep(v, trusted=trusted) for v in value]
    return value


_MCP_MAP = TypeAdapter(dict[str, McpServerSpec])
_AGENT_MAP = TypeAdapter(dict[str, AgentSpec])


class ConnectionRegistry:
    def __init__(
        self,
        allowed_hosts: list[str] | None = None,
        *,
        mcp_servers: dict[str, McpServerSpec] | None = None,
        agents: dict[str, AgentSpec] | None = None,
    ):
        self.allowed_hosts = [h.lower() for h in (allowed_hosts if allowed_hosts is not None else ["*"])]
        self.mcp_servers: dict[str, McpServerSpec] = dict(mcp_servers or {})
        self.agents: dict[str, AgentSpec] = dict(agents or {})

    @classmethod
    def from_file(cls, path: Path | str | None, allowed_hosts: list[str] | None = None) -> ConnectionRegistry:
        if path is None or not Path(path).is_file():
            return cls(allowed_hosts)
        data = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(data, dict):
            raise ConnectionError_(f"{path}: expected a mapping with 'mcp_servers' and/or 'agents'")
        return cls(
            allowed_hosts,
            mcp_servers=_MCP_MAP.validate_python(data.get("mcp_servers") or {}),
            agents=_AGENT_MAP.validate_python(data.get("agents") or {}),
        )

    def check_url(self, url: str) -> None:
        try:
            host = urlsplit(url).hostname
        except ValueError as e:
            raise ConnectionError_(f"invalid URL '{url}': {e}") from None
        if not host:
            raise ConnectionError_(f"invalid URL '{url}': no host")
        host = host.lower()
        if not any(fnmatch.fnmatchcase(host, pattern) for pattern in self.allowed_hosts):
            raise ConnectionError_(f"outbound host '{host}' is not in MAW_ALLOWED_OUTBOUND_HOSTS")

    def mcp(self, name: str, wf: WorkflowDefinition | None = None) -> Resolved[McpServerSpec]:
        if wf is not None and name in wf.mcp_servers:
            resolved = Resolved(name, wf.mcp_servers[name], trusted=False)
        elif name in self.mcp_servers:
            resolved = Resolved(name, self.mcp_servers[name], trusted=True)
        else:
            raise ConnectionError_(f"unknown MCP server '{name}'")
        if resolved.spec.transport == "stdio" and not resolved.trusted:
            raise ConnectionError_(
                f"MCP server '{name}': stdio servers can only be defined in the server-side connections file"
            )
        if resolved.spec.url:
            self.check_url(resolve_secrets(resolved.spec.url, trusted=resolved.trusted))
        return resolved

    def agent(self, name: str, wf: WorkflowDefinition | None = None) -> Resolved[AgentSpec]:
        if wf is not None and name in wf.agents:
            resolved = Resolved(name, wf.agents[name], trusted=False)
        elif name in self.agents:
            resolved = Resolved(name, self.agents[name], trusted=True)
        else:
            raise ConnectionError_(f"unknown agent '{name}'")
        self.check_url(resolve_secrets(resolved.spec.url, trusted=resolved.trusted))
        return resolved

    def describe(self) -> dict[str, Any]:
        """Public view: never includes headers or env (they may hold secrets)."""
        return {
            "mcp_servers": {
                name: {"transport": s.transport, "url": s.url, "command": s.command}
                for name, s in self.mcp_servers.items()
            },
            "agents": {name: {"url": a.url, "description": a.description} for name, a in self.agents.items()},
        }
