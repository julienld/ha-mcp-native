"""Ordinary reloads confirm Core results; self-affecting requests only schedule."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_system_lifecycle import lifecycle as lifecycle


@pytest.fixture
def system(lifecycle: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    world = lifecycle
    world.owner.reply_delay = 60
    world.manager.system_lifecycle = world.owner
    world.manager.entry.state = SimpleNamespace(recoverable=True)
    entry = SimpleNamespace(entry_id="other", state=SimpleNamespace(recoverable=True))
    entries = {"self": world.manager.entry, "other": entry}
    world.manager.hass.config_entries.async_get_entry = entries.get
    world.manager.hass.config_entries.async_reload = AsyncMock(return_value=True)
    world.manager.hass.config_entries.async_schedule_reload = Mock()
    world.manager.hass.services.has_service = lambda domain, service: (
        (domain, service)
        in {
            ("homeassistant", "restart"),
            ("script", "reload"),
            ("automation", "reload"),
        }
    )
    config = AsyncMock(return_value=None)
    monkeypatch.setattr(
        sys.modules["homeassistant"],
        "config",
        SimpleNamespace(async_check_ha_config_file=config),
        raising=False,
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_system_tools"
    )
    operations = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.system_actions"
    )
    monkeypatch.setattr(operations, "Context", SimpleNamespace)
    world.engine = Engine(
        world.manager.backend, module.system_definitions(world.manager)
    )
    world.entry = entry
    world.entries = entries
    world.config = config
    world.migration = False
    world.backup = SimpleNamespace(state="idle")
    world.manager.hass.data = {"backup_manager": world.backup}
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.recorder",
        SimpleNamespace(async_migration_in_progress=lambda hass: world.migration),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.backup",
        SimpleNamespace(async_get_manager=lambda hass: world.backup),
    )
    return world


async def _call(system: Any, name: str, **arguments: Any) -> Any:
    return await system.engine.call(name, arguments, CallContext("actor"))


async def test_restart_without_confirmation_has_no_effect_and_is_not_unknown(
    system: Any,
) -> None:
    result = await _call(system, "ha_restart")
    assert result.is_error and result.status == "not_started", result.data
    system.config.assert_not_awaited()
    assert not system.owner.pending and not system.calls


async def test_invalid_config_does_not_schedule_restart(system: Any) -> None:
    system.config.return_value = "invalid fixture configuration"
    result = await _call(system, "ha_restart", confirm=True)
    assert result.is_error and result.status == "not_started", result.data
    assert result.data["error"]["code"] == "CONFIG_INVALID"
    assert not system.owner.pending and not system.calls


async def test_valid_restart_is_reported_only_as_requested_and_scheduled(
    system: Any,
) -> None:
    result = await _call(system, "ha_restart", confirm=True)
    assert (
        not result.is_error and result.data["requested"] and result.data["scheduled"]
    ), result.data
    assert not system.calls and system.owner.pending
    await system.owner.stop()


async def test_self_entry_reload_does_not_wait_for_its_own_unload(system: Any) -> None:
    result = await _call(system, "ha_reload_core", entry_id="self")
    assert (
        not result.is_error and result.data["scheduled"] and not result.data["reloaded"]
    ), result.data
    system.manager.hass.config_entries.async_reload.assert_not_awaited()
    assert system.owner.pending
    await system.owner.stop()


async def test_ordinary_config_entry_reload_uses_the_real_owner_and_reports_success(
    system: Any,
) -> None:
    result = await _call(system, "ha_reload_core", entry_id="other")
    assert not result.is_error and result.data["reloaded"], result.data
    system.manager.hass.config_entries.async_reload.assert_awaited_once_with("other")
    assert not system.owner.pending


async def test_core_false_without_restart_requirement_is_not_a_false_reload_success(
    system: Any,
) -> None:
    system.manager.hass.config_entries.async_reload.return_value = False
    result = await _call(system, "ha_reload_core", entry_id="other")
    assert result.is_error and result.status == "confirmed", result.data
    assert result.data["reloaded"] is False and result.data["require_restart"] is False


async def test_failed_unload_retains_cores_restart_requirement(system: Any) -> None:
    async def fail(_entry_id: str) -> bool:
        system.entry.state.recoverable = False
        return False

    system.manager.hass.config_entries.async_reload.side_effect = fail
    result = await _call(system, "ha_reload_core", entry_id="other")
    assert (
        not result.is_error
        and result.data["require_restart"]
        and not result.data["reloaded"]
    ), result.data


async def test_reload_service_has_actor_context_and_unknown_after_provider_failure(
    system: Any,
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        system.calls.append((args, kwargs))
        raise RuntimeError("reload failed after effect")

    system.manager.hass.services.async_call.side_effect = fail
    result = await _call(system, "ha_reload_core", target="scripts")
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert len(system.calls) == 1 and system.calls[0][1]["context"].user_id == "actor"


async def test_reload_all_drains_each_available_service_and_preserves_partial_outcomes(
    system: Any,
) -> None:
    async def reload(domain: str, service: str, data: dict, **kwargs: Any) -> None:
        system.calls.append(domain)
        if domain == "script":
            raise RuntimeError("script reload failure")

    system.manager.hass.services.async_call.side_effect = reload
    result = await _call(system, "ha_reload_core")
    assert not result.is_error and result.status == "outcome_unknown", result.data
    assert result.data["reloaded"] == ["automations"] and result.data["partial"]
    assert sorted(system.calls) == ["automation", "script"]


async def test_replaced_entry_manager_cannot_confirm_reload(system: Any) -> None:
    owner = system.manager.hass.config_entries

    async def replace_owner(_entry_id: str) -> bool:
        system.manager.hass.config_entries = SimpleNamespace(
            async_get_entry=system.entries.get
        )
        return True

    owner.async_reload.side_effect = replace_owner
    result = await _call(system, "ha_reload_core", entry_id="other")
    assert result.is_error and result.status == "outcome_unknown", result.data
    owner.async_reload.assert_awaited_once()


async def test_core_cancel_after_entry_reload_dispatch_is_unknown(system: Any) -> None:
    system.manager.hass.config_entries.async_reload.side_effect = asyncio.CancelledError
    result = await _call(system, "ha_reload_core", entry_id="other")
    assert result.is_error and result.status == "outcome_unknown", result.data
    system.manager.hass.config_entries.async_reload.assert_awaited_once()


async def test_revocation_during_config_check_prevents_scheduling(system: Any) -> None:
    async def check() -> None:
        system.manager.backend.authorize.side_effect = PermissionError("revoked")

    system.config.side_effect = check
    result = await _call(system, "ha_restart", confirm=True)
    assert result.is_error and result.status == "not_started", result.data
    assert not system.owner.pending and not system.calls


async def test_repeated_cancellation_drains_the_owned_reload(system: Any) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def reload(_entry_id: str) -> bool:
        entered.set()
        await release.wait()
        return True

    owner = system.manager.hass.config_entries
    owner.async_reload.side_effect = reload
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.system_actions"
    )
    service = module.NativeSystemActions(system.manager, CallContext("actor"))
    task = asyncio.create_task(service.reload("all", "other"))
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    owner.async_reload.assert_awaited_once()


@pytest.mark.parametrize("busy", ["database", "backup"])
async def test_restart_refuses_core_busy_state_before_scheduling(system, busy):
    if busy == "database":
        system.migration = True
    else:
        system.backup.state = "create_backup"
    try:
        result = await _call(system, "ha_restart", confirm=True)
        assert result.is_error and result.status == "not_started", result.data
        assert result.data["error"]["code"] == "RESOURCE_LOCKED"
        assert not system.owner.pending and not system.calls
    finally:
        await system.owner.stop()


async def test_safe_restart_retains_actor_and_warns_server_stays_down(system):
    system.owner.reply_delay = 0
    result = await _call(system, "ha_restart", confirm=True, safe_mode=True)
    assert not result.is_error, result.data
    assert result.data["scheduled"] and "Safe mode" in str(result.data["warnings"])
    await asyncio.gather(*tuple(system.owner.pending))
    await asyncio.gather(*system.tasks)
    args, kwargs, *_ = system.calls[0]
    assert args == ("homeassistant", "restart", {"safe_mode": True})
    assert kwargs["context"].user_id == "actor"


async def test_restart_rechecks_backup_after_reply_before_handoff(system):
    system.owner.reply_delay = 0
    result = await _call(system, "ha_restart", confirm=True)
    assert not result.is_error and result.data["scheduled"]
    system.backup.state = "restore_backup"
    await asyncio.gather(*tuple(system.owner.pending))
    assert not system.calls and not system.owner.pending


async def test_unavailable_backup_manager_is_disclosed_in_restart_warning(
    system, monkeypatch
):
    from homeassistant.exceptions import HomeAssistantError

    def unavailable(hass):
        raise HomeAssistantError("Backup integration is not available")

    monkeypatch.setattr(
        sys.modules["homeassistant.components.backup"], "async_get_manager", unavailable
    )
    try:
        result = await _call(system, "ha_restart", confirm=True)
        assert not result.is_error and result.data["scheduled"]
        assert "Could not read the backup status" in str(result.data["warnings"])
    finally:
        await system.owner.stop()
