"""Settings: .env loading, production-mode guard rails."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from conftest import REPO_ROOT, make_settings

from maw.api.app import InsecureConfigurationError, check_production
from maw.settings import Settings, load_env_file


def test_production_problems_list_every_unsafe_default(tmp_path):
    problems = make_settings(tmp_path, env="production").production_problems()
    joined = "\n".join(problems)
    assert "MAW_API_KEYS" in joined
    assert "MAW_ALLOWED_OUTBOUND_HOSTS" in joined
    assert "MAW_DATABASE_URL" in joined
    assert "MAW_ALLOW_INLINE_API_KEYS" not in joined
    assert "MAW_ALLOW_INLINE_API_KEYS" in "\n".join(
        make_settings(tmp_path, allow_inline_api_keys=True).production_problems()
    )


def test_production_mode_refuses_unsafe_settings(tmp_path):
    with pytest.raises(InsecureConfigurationError, match="refusing to start with MAW_ENV=production"):
        check_production(make_settings(tmp_path, env="production"))
    check_production(make_settings(tmp_path))  # development: anything goes
    safe = make_settings(
        tmp_path,
        env="production",
        api_keys=["k"],
        allowed_outbound_hosts=["api.openai.com", "*.googleapis.com", "mock-mcp", "mock-a2a"],
        database_url="postgresql://maw:x@postgres:5432/maw",
    )
    assert safe.production_problems() == []
    check_production(safe)


def test_load_env_file_exports_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / "custom.env"
    env.write_text("MAW_TEST_DOTENV_A=from-file\nMAW_TEST_DOTENV_B=from-file\n")
    monkeypatch.setenv("MAW_TEST_DOTENV_B", "from-shell")
    monkeypatch.delenv("MAW_TEST_DOTENV_A", raising=False)
    assert load_env_file(env) is True
    assert os.environ["MAW_TEST_DOTENV_A"] == "from-file"
    assert os.environ["MAW_TEST_DOTENV_B"] == "from-shell"  # the shell wins
    monkeypatch.delenv("MAW_TEST_DOTENV_A")
    assert load_env_file(tmp_path / "missing.env") is False


def test_settings_read_env_files_for_maw_values(tmp_path):
    env = tmp_path / "settings.env"
    env.write_text('MAW_PORT=9999\nMAW_API_KEYS=["a","b"]\n')
    settings = Settings(_env_file=env)
    assert settings.port == 9999 and settings.api_keys == ["a", "b"]


def test_cli_exports_provider_keys_from_env_file(tmp_path):
    """`maw` loads .env into os.environ so LangChain sees OPENAI_API_KEY etc."""
    env_file = tmp_path / ".env"
    env_file.write_text("MAW_TEST_PROVIDER_KEY=sk-from-dotenv\n")
    probe = (
        "import os, sys; from maw.main import cli; sys.argv = ['maw', 'schema'];\n"
        "import contextlib, io\n"
        "with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()): cli()\n"
        "print(os.environ.get('MAW_TEST_PROVIDER_KEY'))"
    )
    env = {k: v for k, v in os.environ.items() if k != "MAW_ENV_FILE"}
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "sk-from-dotenv"
    assert REPO_ROOT.exists()
