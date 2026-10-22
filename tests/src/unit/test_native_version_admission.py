"""The native qualification floor must not silently narrow legacy compatibility."""

import builtins
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from .test_config_flow import _make_flow, _make_options_flow, cf, const
from .test_native_server import manager as native_manager


@pytest.fixture(autouse=True)
def isolate_native_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.ha_mcp_tools import runtime_generation

    monkeypatch.setattr(runtime_generation, "_NATIVE_VERSION", None)


@pytest.mark.parametrize("version", ["2026.8.0", "2026.10.1", "unknown"])
async def test_native_creation_refuses_unqualified_core_before_selecting_owner(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setattr(cf, "HA_VERSION", version)
    flow = _make_flow()
    flow.hass.auth.async_get_users = AsyncMock(return_value=[])
    result = await flow.async_step_server(None)
    assert result["type"] == "abort"
    assert result["reason"] == "unsupported_home_assistant"
    assert result["description_placeholders"]["installed"] == version
    assert result["description_placeholders"]["required"] == "2026.10.2"
    flow.async_set_unique_id.assert_not_awaited()
    flow.async_create_entry.assert_not_called()


async def test_options_cannot_switch_an_older_core_to_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cf, "HA_VERSION", "2026.8.0")
    flow = _make_options_flow(options={const.OPT_RUNTIME_MODE: const.RUNTIME_LEGACY})
    result = await flow.async_step_init({const.OPT_RUNTIME_MODE: const.RUNTIME_NATIVE})
    assert result["type"] == "form"
    assert result["errors"][const.OPT_RUNTIME_MODE] == "unsupported_home_assistant"
    flow.async_create_entry.assert_not_called()
    flow.hass.config_entries.async_update_entry.assert_not_called()
    assert flow.config_entry.options[const.OPT_RUNTIME_MODE] == const.RUNTIME_LEGACY


async def test_older_core_still_accepts_legacy_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cf, "HA_VERSION", "2026.8.0")
    flow = _make_options_flow(options={const.OPT_RUNTIME_MODE: const.RUNTIME_LEGACY})
    result = await flow.async_step_init({const.OPT_RUNTIME_MODE: const.RUNTIME_LEGACY})
    assert result["type"] == "entry"
    assert result["data"][const.OPT_RUNTIME_MODE] == const.RUNTIME_LEGACY


@pytest.mark.parametrize("version", ["2026.8.0", "2026.10.1", "unknown"])
def test_imported_native_entry_is_refused_before_loading_native_host(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    from custom_components.ha_mcp_tools import embedded_setup

    monkeypatch.setattr(embedded_setup, "HA_VERSION", version, raising=False)
    original = builtins.__import__

    def no_native_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "native_server" or name.endswith(".native_server"):
            raise AssertionError("Unsupported Core imported the native host")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_native_import)
    with pytest.raises(ValueError, match=r"2026\.10\.2"):
        embedded_setup._server_manager(
            object(),
            SimpleNamespace(options={const.OPT_RUNTIME_MODE: const.RUNTIME_NATIVE}),
        )


async def test_direct_native_start_after_downgrade_refuses_before_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import native_server

    monkeypatch.setattr(native_server, "HA_VERSION", "2026.10.1", raising=False)
    runtime = native_manager()
    runtime.hass.async_add_executor_job = AsyncMock(
        side_effect=AssertionError("Unsupported Core reached modern owner prewarm")
    )
    with pytest.raises(ValueError, match=r"2026\.10\.2"):
        await runtime.async_start()
    runtime.hass.async_add_executor_job.assert_not_awaited()


@pytest.mark.parametrize("options", [{}, {"runtime_mode": "legacy"}])
def test_older_core_keeps_existing_legacy_runtime(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, str]
) -> None:
    from custom_components.ha_mcp_tools import embedded_setup

    monkeypatch.setattr(embedded_setup, "HA_VERSION", "2026.8.0", raising=False)
    legacy = object()
    factory = MagicMock(return_value=legacy)
    monkeypatch.setattr(embedded_setup, "EmbeddedServerManager", factory)
    assert embedded_setup._server_manager(
        object(), SimpleNamespace(options=options)
    ) == (legacy, False)


async def test_qualified_native_core_reaches_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import native_server

    class PrewarmReached(Exception):
        pass

    monkeypatch.setattr(native_server, "HA_VERSION", "2026.10.2", raising=False)
    runtime = native_manager()
    runtime.hass.async_add_executor_job = AsyncMock(side_effect=PrewarmReached)
    with pytest.raises(PrewarmReached):
        await runtime.async_start()
    runtime.hass.async_add_executor_job.assert_awaited_once()
