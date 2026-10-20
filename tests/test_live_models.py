"""Live tests against real LLM and embedding APIs. They cost a few cents and need network access.

Each provider's tests run only when its key is set (in the environment or in .env):
  OPENAI_API_KEY  -> openai (gpt-4o-mini, text-embedding-3-small)
  GOOGLE_API_KEY  -> gemini (gemini-2.5-flash, gemini-embedding-001)

    make live-test        # or: uv run pytest -m live -v
Override models with MAW_LIVE_OPENAI_MODEL / MAW_LIVE_GEMINI_MODEL.
"""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack

import pytest
from conftest import REPO_ROOT, build_service, chain, make_settings, wf
from dotenv import dotenv_values

# Only provider keys and live-test knobs come from .env; MAW_* settings stay out of the tests.
for _name, _value in dotenv_values(REPO_ROOT / ".env").items():
    if _value and (_name in ("OPENAI_API_KEY", "GOOGLE_API_KEY") or _name.startswith("MAW_LIVE_")):
        os.environ.setdefault(_name, _value)

PROVIDERS = {
    "openai": {
        "key": "OPENAI_API_KEY",
        "model": {
            "provider": "openai",
            "model": os.environ.get("MAW_LIVE_OPENAI_MODEL", "gpt-4o-mini"),
            "temperature": 0,
        },
        "embedding": {"provider": "openai", "model": "text-embedding-3-small"},
    },
    "gemini": {
        "key": "GOOGLE_API_KEY",
        "model": {
            "provider": "google_genai",
            "model": os.environ.get("MAW_LIVE_GEMINI_MODEL", "gemini-2.5-flash"),
            "temperature": 0,
        },  # fmt: skip
        "embedding": {
            "provider": "google_genai",
            "model": "models/gemini-embedding-001",
            "params": {"output_dimensionality": 768},
        },  # fmt: skip
    },
}

pytestmark = pytest.mark.live


@pytest.fixture(params=list(PROVIDERS))
def provider(request):
    config = PROVIDERS[request.param]
    if not os.environ.get(config["key"]):
        pytest.skip(f"{config['key']} is not set")
    return config


async def run(service, raw, input=""):
    definition, _ = service.resolve(workflow=raw)
    return await service.start(definition, input)


async def test_plain_answer(service, provider):
    raw = wf([{"id": "a", "type": "llm", "model": "m", "system_prompt": "Reply in at most five words."}], chain("a"),
             models={"m": provider["model"]})  # fmt: skip
    record = await run(service, raw, "Say hello to the MAW test suite.")
    assert record.status == "completed", record.error
    assert record.text and record.trace[0]["model"].endswith(provider["model"]["model"])


async def test_structured_classification(service, provider):
    raw = wf(
        [{"id": "classify", "type": "llm", "model": "m",
          "system_prompt": "Classify the employee request. category: billing | it | hr | general.",
          "output_schema": {"type": "object", "properties": {
              "category": {"type": "string", "enum": ["billing", "it", "hr", "general"]}}, "required": ["category"]}}],
        chain("classify"),
        models={"m": provider["model"]},
    )  # fmt: skip
    for question, category in [
        ("Was invoice INV-1001 charged twice?", "billing"),
        ("My VPN password expired", "it"),
    ]:
        record = await run(service, raw, question)
        assert record.status == "completed", record.error
        assert record.output["category"] == category


async def test_tool_loop_with_calculator(service, provider):
    raw = wf(
        [{"id": "calc", "type": "llm", "model": "m", "tools": {"functions": ["calculator"]}, "max_tool_calls": 3,
          "system_prompt": "Always use the calculator tool for arithmetic, then state the result."}],
        chain("calc"),
        models={"m": provider["model"]},
    )  # fmt: skip
    record = await run(service, raw, "What is 1234 * 5678?")
    assert record.status == "completed", record.error
    assert record.trace[0]["tool_calls"] >= 1
    assert "7006652" in record.text.replace(",", "").replace(" ", "")


async def test_rag_with_real_embeddings(tmp_path, provider):
    connections = tmp_path / "live-connections.yaml"
    connections.write_text(
        "vector_stores:\n  docs:\n    provider: memory\n    collection: live\n"
        f"    embedding: {json.dumps(provider['embedding'])}\n"
    )
    async with AsyncExitStack() as stack:
        service = await build_service(
            make_settings(tmp_path, workflows_dir=None, connections_file=connections), stack
        )
        store = service.ctx.connections.vector_store("docs")
        docs = [{"id": p.name, "text": p.read_text(), "metadata": {"source": p.name}}
                for p in sorted((REPO_ROOT / "examples" / "docs").glob("*.md"))]  # fmt: skip
        await service.ctx.vectors.add_documents(store, docs)
        # Semantic, not keyword: the question shares almost no words with the leave policy.
        hits = await service.ctx.vectors.search(
            store, "How much time off am I entitled to each year?", top_k=1
        )
        assert hits[0]["metadata"]["source"] == "annual-leave.md"

        raw = json.loads((REPO_ROOT / "examples" / "rag_qa_gemini.json").read_text())
        raw["models"] = {"gemini": provider["model"]}
        raw["nodes"][0]["store"] = "docs"
        record = await run(service, raw, "How many days of annual leave do full-time staff get?")
        assert record.status == "completed", record.error
        assert "24" in record.text


async def test_support_triage_end_to_end(live_service, provider):
    raw = json.loads((REPO_ROOT / "workflows" / "support_triage.json").read_text())
    raw["id"] = "live_support_triage"
    raw["models"] = {"router": provider["model"], "it_assistant": provider["model"]}
    for question, route in [
        ("Was INV-1001 charged twice?", ["classify", "billing", "respond"]),
        ("My VPN password expired, how do I reset it?", ["classify", "it_support", "respond"]),
    ]:
        record = await run(live_service, raw, question)
        assert record.status == "completed", record.error
        assert [t["node"] for t in record.trace] == route
    assert record.trace[1]["tool_calls"] >= 1  # the IT assistant searched the knowledge base
