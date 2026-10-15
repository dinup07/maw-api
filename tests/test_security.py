"""Security controls: secrets, trust levels, stdio, host allowlist, model config, calculator."""

from __future__ import annotations

import asyncio

import pytest
from conftest import _spawn, _stop, chain, free_port, wf

from maw.connectors import A2AConnector, ConnectionError_, ConnectionRegistry, resolve_secrets
from maw.engine.models import ModelConfigError, ModelRegistry
from maw.functions import get_function
from maw.schema import parse_workflow
from maw.schema.workflow import AgentSpec


def definition(**extra):
    parsed, _ = parse_workflow(wf([{"id": "a", "type": "transform", "mapping": {}}], chain("a"), **extra))
    return parsed


# 1 ---------------------------------------------------------------------------------------------------
def test_secrets_read_only_maw_secret_vars(monkeypatch):
    monkeypatch.setenv("MAW_SECRET_TOKEN", "t0k3n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.delenv("MAW_SECRET_OPENAI_API_KEY", raising=False)
    assert resolve_secrets("Bearer ${secret:TOKEN}", trusted=False) == "Bearer t0k3n"
    with pytest.raises(ConnectionError_, match="expected env var MAW_SECRET_OPENAI_API_KEY"):
        resolve_secrets("${secret:OPENAI_API_KEY}", trusted=False)


# 2 ---------------------------------------------------------------------------------------------------
def test_env_only_in_trusted_config(monkeypatch):
    monkeypatch.setenv("KB_HOST", "kb.internal")
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert resolve_secrets("http://${env:KB_HOST}/mcp", trusted=True) == "http://kb.internal/mcp"
    assert resolve_secrets("${env:UNSET_VAR:-http://fallback}", trusted=True) == "http://fallback"
    with pytest.raises(ConnectionError_, match="not set"):
        resolve_secrets("${env:UNSET_VAR}", trusted=True)
    with pytest.raises(ConnectionError_, match=r"only allowed in the server-side connections file"):
        resolve_secrets("${env:KB_HOST}", trusted=False)

    registry = ConnectionRegistry()
    sneaky = definition(mcp_servers={"x": {"url": "http://${env:KB_HOST}/mcp"}})
    with pytest.raises(ConnectionError_, match="only allowed"):
        registry.mcp("x", sneaky)


# 3 ---------------------------------------------------------------------------------------------------
def test_stdio_only_from_trusted_file(tmp_path):
    untrusted = definition(
        mcp_servers={"shell": {"transport": "stdio", "command": "bash", "args": ["-c", "id"]}}
    )
    with pytest.raises(ConnectionError_, match="stdio servers can only be defined in the server-side"):
        ConnectionRegistry().mcp("shell", untrusted)

    path = tmp_path / "connections.yaml"
    path.write_text("mcp_servers:\n  fs:\n    transport: stdio\n    command: npx\n    args: [server]\n")
    resolved = ConnectionRegistry.from_file(path).mcp("fs")
    assert resolved.trusted is True and resolved.spec.command == "npx"


# 4 ---------------------------------------------------------------------------------------------------
def test_outbound_host_allowlist():
    registry = ConnectionRegistry(["*.corp.example", "localhost"])
    registry.check_url("https://kb.corp.example/mcp")
    registry.check_url("http://LOCALHOST:8001/mcp")
    for url in ("https://evil.example/x", "https://corp.example.evil.io/", "http://169.254.169.254/latest"):
        with pytest.raises(ConnectionError_, match="not in MAW_ALLOWED_OUTBOUND_HOSTS"):
            registry.check_url(url)
    with pytest.raises(ConnectionError_, match="invalid URL"):
        registry.check_url("not a url")

    inline = definition(
        agents={"bad": {"url": "https://evil.example"}, "ok": {"url": "https://billing.corp.example"}},
        mcp_servers={"bad": {"url": "https://evil.example/mcp"}},
    )
    assert registry.agent("ok", inline).trusted is False
    with pytest.raises(ConnectionError_, match="evil.example"):
        registry.agent("bad", inline)
    with pytest.raises(ConnectionError_, match="evil.example"):
        registry.mcp("bad", inline)


async def test_agent_card_interfaces_are_allowlist_checked():
    """An allowed agent whose card points elsewhere must be refused."""
    port = free_port()
    proc = _spawn(
        [
            "mocks/a2a_billing_agent.py",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--public-url",
            f"http://localhost:{port}",
        ],
        f"http://127.0.0.1:{port}/.well-known/agent-card.json",
    )
    try:
        registry = ConnectionRegistry(
            ["127.0.0.1"], agents={"billing": AgentSpec(url=f"http://127.0.0.1:{port}")}
        )
        connector = A2AConnector(registry)
        with pytest.raises(ConnectionError_, match="outbound host 'localhost'"):
            await connector.card(registry.agent("billing"))
        await connector.aclose()
    finally:
        await asyncio.to_thread(_stop, proc)


# 5 ---------------------------------------------------------------------------------------------------
def test_model_base_url_and_literal_api_key(monkeypatch):
    registry = ModelRegistry(ConnectionRegistry(["api.openai.com"]))
    outside = definition(models={"m": {"provider": "openai", "model": "gpt-4o-mini", "base_url": "https://evil.example/v1",
                                       "api_key": "${secret:K}"}})  # fmt: skip
    with pytest.raises(ConnectionError_, match="evil.example"):
        registry.get("m", outside)

    literal = definition(models={"m": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-pasted"}})
    with pytest.raises(ModelConfigError, match="literal api_key is disabled"):
        registry.get("m", literal)

    monkeypatch.setenv("MAW_SECRET_K", "sk-from-secret")
    model, label = registry.get("m", definition(models={"m": {"provider": "openai", "model": "gpt-4o-mini",
                                                             "api_key": "${secret:K}"}}))  # fmt: skip
    assert label == "openai:gpt-4o-mini"
    assert model.openai_api_key.get_secret_value() == "sk-from-secret"

    permissive = ModelRegistry(ConnectionRegistry(), allow_inline_api_keys=True)
    model, _ = permissive.get("m", literal)
    assert model.openai_api_key.get_secret_value() == "sk-pasted"


# 6 ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("expression", "result"),
    [("2 ** 10 + 1", 1025), ("(1 + 2) * 3 / 2", 4.5), ("-7 // 2", -4), ("10 % 4", 2)],
)
async def test_calculator_evaluates(expression, result):
    assert (await get_function("calculator").call(expression=expression))["result"] == result


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('id')",
        "(1).__class__",
        "9 ** 9 ** 9",
        "True + 1",
        "x + 1",
        "[1, 2]",
        "1" * 501,
        "",
    ],
)
async def test_calculator_rejects_unsafe_input(expression):
    with pytest.raises(ValueError):
        await get_function("calculator").call(expression=expression)
