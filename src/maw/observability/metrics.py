"""Prometheus metrics. Labels stay low-cardinality: never run ids or user input."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

runs_total = Counter("maw_runs_total", "Finished runs", ["workflow_id", "outcome"])
run_duration_seconds = Histogram("maw_run_duration_seconds", "Run duration", ["workflow_id"], buckets=BUCKETS)
runs_in_flight = Gauge("maw_runs_in_flight", "Runs currently executing")

node_duration_seconds = Histogram(
    "maw_node_duration_seconds", "Node duration", ["node_type", "outcome"], buckets=BUCKETS
)
node_retries_total = Counter("maw_node_retries_total", "Node retries", ["node_type"])

llm_calls_total = Counter("maw_llm_calls_total", "LLM calls", ["model", "outcome"])
llm_call_duration_seconds = Histogram(
    "maw_llm_call_duration_seconds", "LLM call duration", ["model"], buckets=BUCKETS
)
llm_tokens_total = Counter("maw_llm_tokens_total", "LLM tokens", ["model", "direction"])

mcp_calls_total = Counter("maw_mcp_calls_total", "MCP tool calls", ["server", "outcome"])
mcp_call_duration_seconds = Histogram(
    "maw_mcp_call_duration_seconds", "MCP call duration", ["server"], buckets=BUCKETS
)

a2a_calls_total = Counter("maw_a2a_calls_total", "A2A calls", ["agent", "outcome"])
a2a_call_duration_seconds = Histogram(
    "maw_a2a_call_duration_seconds", "A2A call duration", ["agent"], buckets=BUCKETS
)

graph_cache_total = Counter("maw_graph_cache_total", "Compiled-graph cache lookups", ["result"])
graph_compile_seconds = Histogram("maw_graph_compile_seconds", "Workflow compile time", buckets=BUCKETS)

vector_queries_total = Counter("maw_vector_queries_total", "Vector store searches", ["store", "outcome"])
vector_query_duration_seconds = Histogram(
    "maw_vector_query_duration_seconds",
    "Vector search duration (embedding + query)",
    ["store"],
    buckets=BUCKETS,
)
vector_upserts_total = Counter("maw_vector_upserts_total", "Chunks written to vector stores", ["store"])
