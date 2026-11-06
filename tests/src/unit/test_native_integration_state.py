"""Native integration state ports use Core owners after fresh admission."""

import sys
from enum import StrEnum
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.flows.session import FlowSession
from ha_mcp.runtime.integration_state import set_entry_enabled, set_log_level
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_helper_collections import FakeCollection, _hass
from .test_component_ws_search import _REAL_VOL as vol
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


@pytest.mark.parametrize("revoke", [False, True])
async def test_native_logger_uses_its_core_settings_and_rechecks_after_loader(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    revoke: bool,
) -> None:
    hass = _hass(FakeCollection())
    actor = authorize(hass)
    update = AsyncMock()
    hass.data["logger"] = SimpleNamespace(settings=SimpleNamespace(async_update=update))
    hass.data["websocket_api"]["logger/integration_log_level"] = (
        None,
        vol.Schema(
            {
                "id": int,
                "type": str,
                "integration": str,
                "level": vol.In(["DEBUG", "NOTSET"]),
                "persistence": "once",
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.logger.helpers",
        SimpleNamespace(
            DATA_LOGGER="logger",
            LoggerSetting=SimpleNamespace,
            LogSettingsType=SimpleNamespace(INTEGRATION="integration"),
        ),
    )

    async def load(*_args: Any) -> Any:
        actor.is_active = not revoke
        return object()

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(
            IntegrationNotFound=type("IntegrationNotFound", (Exception,), {}),
            async_get_integration=load,
        ),
    )
    backend = native.NativeBackend(hass)
    session = FlowSession(backend, CallContext("actor"))
    if revoke:
        with pytest.raises(ToolFailure, match="admission was revoked"):
            await set_log_level(session, None, "hue", "DEBUG", other_modes=False)
        update.assert_not_awaited()
        return
    result = await set_log_level(session, None, "hue", "DEBUG", other_modes=False)
    assert result["success"] and result["log_level"] == "DEBUG"
    update.assert_awaited_once()
    assert update.await_args.args[:2] == (hass, "hue")
    setting = update.await_args.args[2]
    assert (setting.level, setting.persistence, setting.type) == (
        "DEBUG",
        "once",
        "integration",
    )


@pytest.mark.parametrize("refuses_reload", [False, True])
async def test_native_enable_uses_core_owner_and_reports_restart_requirement(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    refuses_reload: bool,
) -> None:
    class Disabler(StrEnum):
        USER = "user"

    denied = type("OperationNotAllowed", (Exception,), {})
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(
            ConfigEntryDisabler=Disabler,
            OperationNotAllowed=denied,
            UnknownEntry=type("UnknownEntry", (Exception,), {}),
        ),
    )
    hass = _hass(FakeCollection())
    authorize(hass)
    metadata = {"entry_id": "entry", "domain": "hue", "modified_at": 1}
    entry = SimpleNamespace(domain="hue", as_json_fragment=metadata)
    change = AsyncMock(
        side_effect=denied() if refuses_reload else None, return_value=True
    )
    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda key: entry, async_set_disabled_by=change
    )
    hass.data["websocket_api"]["config_entries/disable"] = (
        None,
        vol.Schema(
            {
                "id": int,
                "type": str,
                "entry_id": str,
                "disabled_by": vol.Any("user", None),
            }
        ),
    )
    backend = native.NativeBackend(hass)
    result = await set_entry_enabled(
        FlowSession(backend, CallContext("actor")), "entry", False
    )
    assert result["success"]
    assert result["require_restart"] is refuses_reload
    change.assert_awaited_once_with("entry", Disabler.USER)
