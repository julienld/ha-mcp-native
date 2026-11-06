"""Standalone OAuth startup must load its configured dotenv before environment reads."""

import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest


def _clear_startup_env(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    # Record absence too: main_oauth/setdefault and dotenv can introduce the key.
    monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.delenv(key)


@pytest.mark.parametrize(
    "startup_test",
    [
        "test_invalid_oauth_settings_use_the_existing_configuration_error_handler",
        "test_oauth_uses_dotenv_urls_and_preserves_token_sentinel",
    ],
)
def test_startup_checks_restore_originally_absent_environment(
    tmp_path, monkeypatch, startup_test
):
    keys = (
        "HOMEASSISTANT_URL",
        "MCP_BASE_URL",
        "HOMEASSISTANT_TOKEN",
        "MCP_HOST",
        "MCP_PORT",
    )
    for key in keys:
        monkeypatch.setenv(key, "outer-isolation")
        monkeypatch.delenv(key)
    with pytest.MonkeyPatch.context() as isolated:
        globals()[startup_test](tmp_path, isolated)
    assert {key: os.environ[key] for key in keys if key in os.environ} == {}


def test_invalid_oauth_settings_use_the_existing_configuration_error_handler(
    tmp_path, monkeypatch
):
    from pydantic import ValidationError

    from ha_mcp import __main__ as main
    from ha_mcp import config_settings

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(config_settings, "env_path", env_file)
    monkeypatch.setenv("HOMEASSISTANT_URL", "ftp://ha.test")
    monkeypatch.setenv("MCP_BASE_URL", "https://mcp.test")
    _clear_startup_env(monkeypatch, "HOMEASSISTANT_TOKEN")
    errors = []

    def handle(error):
        errors.append(error)
        raise SystemExit(1)

    monkeypatch.setattr(main, "_handle_config_error", handle)
    run = Mock()
    monkeypatch.setattr(main, "_run_entrypoint", run)
    with pytest.raises(SystemExit):
        main.main_oauth()
    assert len(errors) == 1 and isinstance(errors[0], ValidationError)
    run.assert_not_called()


def test_oauth_uses_dotenv_urls_and_preserves_token_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_mcp import __main__ as main
    from ha_mcp import config_settings
    from ha_mcp.config import OAUTH_MODE_TOKEN

    env_file = tmp_path / ".env"
    env_file.write_text(
        "HOMEASSISTANT_URL=http://ha.test:8123\nMCP_BASE_URL=https://mcp.test\n"
        "MCP_HOST=127.0.0.2\nMCP_PORT=9001\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_settings, "env_path", env_file)
    for key in (
        "HOMEASSISTANT_URL",
        "MCP_BASE_URL",
        "HOMEASSISTANT_TOKEN",
        "MCP_HOST",
        "MCP_PORT",
    ):
        _clear_startup_env(monkeypatch, key)
    monkeypatch.setattr(main, "_log_startup_version", Mock())
    run = AsyncMock()
    monkeypatch.setattr(main, "_run_oauth_server", run)

    def consume(coro: Coroutine[Any, Any, None], _description: str) -> None:
        coro.close()

    monkeypatch.setattr(main, "_run_entrypoint", consume)
    main.main_oauth()
    run.assert_called_once_with(
        "http://ha.test:8123", "https://mcp.test", "127.0.0.2", 9001, "/mcp"
    )
    assert main.os.environ["HOMEASSISTANT_TOKEN"] == OAUTH_MODE_TOKEN
