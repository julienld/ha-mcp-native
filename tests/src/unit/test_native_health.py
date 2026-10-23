"""Native health sections keep Core owners, independent failures and shared projection."""

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_health_info import health as health_info  # noqa: F401 - shared pytest fixture


@pytest.fixture
async def health(health_info: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811 - pytest fixture injection
    base = health_info
    base.rows["homeassistant"] = {"info": {"healthy": True}, "manage_url": "/config"}
    hass = base.hass
    hass.config = SimpleNamespace(
        components={"zha", "zwave_js", "otbr"}, safe_mode=False, recovery_mode=False
    )

    async def execute(function: Any, *arguments: Any) -> Any:
        return await asyncio.to_thread(function, *arguments)

    hass.async_add_executor_job = execute
    snapshot = {
        "issues": [
            {"issue_id": "active", "active": True, "future": True},
            {"issue_id": "ignored", "ignored": True},
            {"issue_id": "inactive", "active": False},
        ],
        "config_entries": [
            {"entry_id": "zwave", "domain": "zwave_js", "state": "loaded"},
            {
                "entry_id": "matter",
                "domain": "matter",
                "state": "loaded",
                "title": "Native Matter",
            },
        ],
        "entities": [
            {"entity_id": "sensor.orphan", "config_entry_id": "removed"},
            {"entity_id": "sensor.restored", "config_entry_id": "matter"},
        ],
        "states": [
            {
                "entity_id": "sensor.restored",
                "state": "unavailable",
                "attributes": {"restored": True},
            }
        ],
    }
    system = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.system"
    )
    capture = Mock(return_value=snapshot)
    monkeypatch.setattr(system, "_do_system_snapshot", capture)
    loaded = object()
    controller = SimpleNamespace(
        home_id=7,
        sdk_version="test-sdk",
        own_node_id=1,
        is_primary=True,
        is_using_home_id_from_other_network=False,
        was_real_primary=True,
        is_suc=True,
        node_type=1,
        firmware_version="test-firmware",
        manufacturer_id=1,
        product_id=2,
        product_type=3,
        supported_function_types={1},
        suc_node_id=1,
        supports_timers=True,
        supports_long_range=False,
        is_rebuilding_routes=False,
        inclusion_state=0,
        rf_region=1,
        status="ready",
        controller_type="primary",
        is_SIS_present=True,
        nodes={9: SimpleNamespace(node_id=9)},
        async_get_state=AsyncMock(return_value={"fresh": True}),
        update=Mock(),
    )
    zwave = SimpleNamespace(
        entry_id="zwave",
        state=loaded,
        runtime_data=SimpleNamespace(
            client=SimpleNamespace(driver=SimpleNamespace(controller=controller))
        ),
    )
    border = SimpleNamespace(
        get_border_agent_id=AsyncMock(return_value=b"\x01"),
        get_active_dataset=AsyncMock(
            return_value=SimpleNamespace(channel=15, extended_pan_id="ABCD")
        ),
        get_extended_address=AsyncMock(return_value=b"\x02"),
        get_active_dataset_tlvs=Mock(
            side_effect=AssertionError("Health must not read network key TLVs")
        ),
    )
    thread = SimpleNamespace(entry_id="otbr", state=loaded, runtime_data=border)
    entries = {"zwave": zwave, "otbr": thread}
    hass.config_entries = SimpleNamespace(
        async_get_entry=entries.get,
        async_loaded_entries=lambda domain: [thread] if domain == "otbr" else [],
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.zwave_js.api",
        SimpleNamespace(
            node_status=lambda node: {
                "node_id": node.node_id,
                "status": "alive",
                "future_node_field": True,
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.otbr.const",
        SimpleNamespace(DOMAIN="otbr"),
    )
    gateway = SimpleNamespace(
        device_proxies={
            "device": SimpleNamespace(
                zha_device_info={
                    "ieee": "01:02",
                    "user_given_name": "ZHA device",
                    "future_device_field": True,
                }
            )
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.zha.helpers",
        SimpleNamespace(get_zha_gateway_proxy=lambda _: gateway),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.frontend",
        SimpleNamespace(
            DATA_THEMES="themes",
            DATA_DEFAULT_THEME="default_theme",
            DATA_DEFAULT_DARK_THEME="dark_theme",
        ),
    )
    hass.data.update(
        themes={"alpha": {"large": "theme"}, "beta": {}},
        default_theme="alpha",
        dark_theme="beta",
    )
    config = AsyncMock(
        return_value=SimpleNamespace(
            errors=[], error_str=None, warning_str="Core config warning"
        )
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "check_config",
        SimpleNamespace(async_check_ha_config_file=config),
        raising=False,
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.health"
    )
    monkeypatch.setattr(
        module, "json_bytes", lambda value: json.dumps(value, default=list).encode()
    )
    radios = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.health_radios"
    )
    monkeypatch.setattr(radios, "ConfigEntryState", SimpleNamespace(LOADED=loaded))
    diagnostic = AsyncMock(
        return_value={
            "data": {"devices": [{"id": 1}, {"id": 2}]},
            "home_assistant": {"version": "native"},
        }
    )
    monkeypatch.setattr(module, "diagnostics", diagnostic)
    binder = importlib.import_module(
        "custom_components.ha_mcp_tools.native_health_tools"
    )
    definition = binder.health_definition(
        SimpleNamespace(hass=hass, backend=base.backend)
    )
    return SimpleNamespace(
        engine=Engine(base.backend, [definition]),
        user=base.user,
        hass=hass,
        snapshot=snapshot,
        capture=capture,
        controller=controller,
        border=border,
        entries=entries,
        config=config,
        diagnostic=diagnostic,
        module=module,
    )


async def _get(health: Any, include: str | None = None, **arguments: Any) -> Any:
    return await health.engine.call(
        "ha_get_system_health",
        {"include": include, **arguments},
        CallContext("actor", read_only=True),
    )


async def test_health_aggregates_one_core_snapshot_radio_owners_and_config_warnings(
    health: Any,
) -> None:
    result = await _get(
        health,
        "repairs,zha_network_full,zwave_network,thread_network,matter_network,themes,dead_entities,config_check",
    )
    assert not result.is_error, result.data
    data = result.data
    assert (
        data["component_count"] == 1
        and data["health_info"]["data"]["homeassistant"]["manage_url"] == "/config"
    )
    assert [issue["issue_id"] for issue in data["repairs"]["issues"]] == ["active"]
    assert (
        data["repairs"]["issues"][0]["future"]
        and data["repairs"]["dismissed_count"] == 1
    )
    assert data["zha_network"]["devices"][0]["future_device_field"]
    assert "error" not in data["zwave_network"], data["zwave_network"]
    assert data["zwave_network"]["nodes"][0]["node_id"] == 9
    assert data["zwave_network"]["controller"]["nodes"][0]["future_node_field"]
    health.controller.async_get_state.assert_awaited_once()
    health.controller.update.assert_called_once_with({"fresh": True})
    assert data["thread_network"]["border_routers"] == [
        {
            "extended_address": "02",
            "channel": 15,
            "extended_pan_id": "abcd",
            "border_agent_id": "01",
        }
    ]
    health.border.get_active_dataset_tlvs.assert_not_called()
    assert data["matter_network"] == {
        "config_entry_id": "matter",
        "state": "loaded",
        "title": "Native Matter",
    }
    assert (
        data["themes"]["themes"] == ["alpha", "beta"]
        and data["themes"]["default_dark_theme"] == "beta"
    )
    assert data["dead_entities"]["summary"]["candidate_total"] == 2
    assert data["dead_entities"]["config_entries_checked"] is True
    assert (
        data["config_check"]["is_valid"]
        and data["config_check"]["warnings"] == "Core config warning"
    )
    assert "ha_mcp_update" not in data
    health.capture.assert_called_once()


async def test_health_baseline_absence_does_not_disable_independent_core_sections(
    health: Any,
) -> None:
    del health.hass.data["health_platforms"]
    result = await _get(health, "repairs,config_check")
    assert not result.is_error and result.data["baseline_available"] is False, (
        result.data
    )
    assert (
        result.data["repairs"]["count"] == 1 and result.data["config_check"]["is_valid"]
    )
    bare = await _get(health)
    assert bare.is_error and bare.data["error"]["code"] == "UNSUPPORTED_OPERATION"


async def test_health_snapshot_failure_is_explicit_and_sibling_section_still_runs(
    health: Any,
) -> None:
    health.capture.side_effect = RuntimeError("snapshot unavailable")
    result = await _get(health, "repairs,config_check")
    assert not result.is_error, result.data
    assert "snapshot unavailable" in result.data["repairs"]["error"]
    assert result.data["config_check"]["is_valid"]


async def test_health_diagnostics_uses_shared_projection_and_exact_core_target(
    health: Any,
) -> None:
    result = await _get(
        health,
        "diagnostics",
        config_entry_id="entry",
        device_id="device",
        diagnostics_fields=["data"],
        diagnostics_data_path="data.devices",
        diagnostics_data_offset=1,
        diagnostics_data_limit=1,
    )
    assert not result.is_error, result.data
    assert result.data["diagnostics"]["data"]["items"] == [{"id": 2}]
    assert (
        result.data["diagnostics"]["data"]["offset"] == 1
        and result.data["diagnostics"]["data"]["total"] == 2
    )
    health.diagnostic.assert_awaited_once_with(
        health.hass, {"entry_id": "entry", "device_id": "device", "timeout": 60}
    )


async def test_health_empty_registry_does_not_assert_orphans_without_live_integrations(
    health: Any,
) -> None:
    health.snapshot["config_entries"] = []
    result = await _get(health, "dead_entities")
    assert not result.is_error, result.data
    assert result.data["dead_entities"]["config_entries_checked"] is False
    assert result.data["dead_entities"]["config_entry_orphans"]["count"] == 0
    assert result.data["dead_entities"]["stale_restored"]["count"] == 1
    assert result.data["warnings"]


async def test_health_failed_diagnostics_keeps_the_requested_target(
    health: Any,
) -> None:
    health.diagnostic.side_effect = TimeoutError("provider did not finish")
    result = await _get(
        health, "diagnostics", config_entry_id="entry", device_id="device"
    )
    assert not result.is_error, result.data
    detail = result.data["diagnostics"]
    assert detail["config_entry_id"] == "entry" and detail["device_id"] == "device"
    assert "provider did not finish" in detail["error"] and "data" not in detail


async def test_health_revalidation_precedes_radio_cache_update_and_disclosure(
    health: Any,
) -> None:
    async def revoke() -> dict:
        health.user.is_active = False
        return {"fresh": True}

    health.controller.async_get_state.side_effect = revoke
    result = await _get(health, "zwave_network")
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN"
    health.controller.update.assert_not_called()


async def test_health_fatal_section_waits_for_other_owned_reads_to_drain(
    health: Any,
) -> None:
    started, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fail(_hass: Any) -> None:
        await started.wait()
        raise ToolFailure("AUTH_FORBIDDEN", "admission revoked", status="not_started")

    async def pending(*_args: Any) -> dict:
        started.set()
        await release.wait()
        drained.set()
        return {"private": "value"}

    health.config.side_effect = fail
    health.diagnostic.side_effect = pending
    task = asyncio.create_task(
        _get(health, "config_check,diagnostics", config_entry_id="entry")
    )
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert drained.is_set() and "private" not in str(result.data)
