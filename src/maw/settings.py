"""Runtime configuration, read from ``MAW_*`` environment variables (and an optional ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAW_", env_file=".env", extra="ignore")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = "http://localhost:8000"
    api_keys: list[str] = []
    log_level: str = "INFO"
    log_json: bool = True

    # Persistence
    database_url: str | None = None
    resume_on_startup: bool = True

    # Catalog and connections
    workflows_dir: Path | None = Path("workflows")
    connections_file: Path | None = Path("config/connections.yaml")
    allow_workflow_registration: bool = True

    # Engine defaults and limits
    default_recursion_limit: int = 50
    default_max_node_visits: int = 10
    default_max_tool_calls: int = 8
    graph_cache_size: int = 128
    mcp_tool_cache_ttl_s: float = 300
    a2a_card_cache_ttl_s: float = 300
    outbound_timeout_s: float = 60
    max_concurrent_runs: int = 64

    # Security
    allowed_outbound_hosts: list[str] = ["*"]
    allow_inline_api_keys: bool = False

    # Extensions
    function_modules: list[str] = []

    # Observability
    metrics_enabled: bool = True
    tracing_enabled: bool = False
    service_name: str = "maw"


@lru_cache
def get_settings() -> Settings:
    return Settings()
