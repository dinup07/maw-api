"""Runtime configuration, read from ``MAW_*`` environment variables (and an optional ``.env``)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# MAW_ENV_FILE points at another env file (or a missing path to read none). Default: ./.env
ENV_FILE = os.environ.get("MAW_ENV_FILE", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAW_", env_file=ENV_FILE, extra="ignore")

    # "production" refuses to start with development-only defaults (see production_problems()).
    env: Literal["development", "production"] = "development"

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

    def production_problems(self) -> list[str]:
        """Settings that are fine for development but unsafe on a shared, reachable server."""
        problems = []
        if not self.api_keys:
            problems.append("MAW_API_KEYS is empty: every endpoint would be public")
        if "*" in self.allowed_outbound_hosts:
            problems.append(
                "MAW_ALLOWED_OUTBOUND_HOSTS allows '*': submitted workflows could make MAW call any host"
            )
        if self.allow_inline_api_keys:
            problems.append(
                "MAW_ALLOW_INLINE_API_KEYS=true: provider keys could be pasted into workflow JSON"
            )
        if not self.database_url:
            problems.append("MAW_DATABASE_URL is not set: runs would be lost on restart")
        return problems


def load_env_file(path: str | Path | None = None) -> bool:
    """Export a .env file into the process environment (existing variables win).

    pydantic-settings already reads MAW_* settings from .env, but provider keys (OPENAI_API_KEY,
    GOOGLE_API_KEY, ...), MAW_SECRET_* and ${env:...} references are read from os.environ.
    """
    from dotenv import load_dotenv

    path = Path(path or os.environ.get("MAW_ENV_FILE", ".env"))
    return load_dotenv(path, override=False) if path.is_file() else False


@lru_cache
def get_settings() -> Settings:
    return Settings()
