"""Native overview filters its complete image before aggregates and pagination."""

import asyncio
import importlib
import threading
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.overview_tools import OverviewTools
from ha_mcp.tool_api import CallContext
from ha_mcp.visibility.model import VisibilityConfig

from .test_component_ws_search import FakeHass, FakeServices, FakeState
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


def _setup(native: Any, monkeypatch: pytest.MonkeyPatch) -> tuple:
    config = {
        "version": "fixture",
        "location_name": "Test",
        "time_zone": "UTC",
        "future_core_config": {"kept": False},
    }
    hass = FakeHass(
        states=[
            FakeState("light.visible"),
            FakeState("light.hidden"),
            FakeState("sensor.one", "2"),
        ],
        config=SimpleNamespace(as_dict=lambda: dict(config)),
        services=FakeServices({"light": {"turn_on": {}, "turn_off": {}}}),
        data={
            "persistent_notification": {
                "one": {
                    "notification_id": "one",
                    "title": "A notice",
                    "message": "Body",
                }
            }
        },
    )
    authorize(hass)
    manager = SimpleNamespace(
        hass=hass,
        backend=native.NativeBackend(hass),
        visibility=SimpleNamespace(_config=AsyncMock(return_value=None)),
        policy=SimpleNamespace(
            read_only=False,
            pinned_tools={"ha_get_state"},
            flag=lambda name: name == "enable_tool_search",
            tool_enabled=lambda definition: True,
        ),
    )
    manager.backend.execute = AsyncMock(
        side_effect=AssertionError("No transport dispatcher")
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.overview"
    )
    monkeypatch.setattr(
        module.NativeOverview, "_base_url", lambda self: "http://ha.invalid"
    )
    bindings = importlib.import_module(
        "custom_components.ha_mcp_tools.native_overview_tools"
    )
    engine = Engine(manager.backend, [bindings.overview_definition(manager)])
    manager.engine = engine
    return engine, manager, module


async def test_native_overview_filters_decluttered_entities_before_counts_pages_and_executor(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, manager, module = _setup(native, monkeypatch)
    manager.visibility._config.return_value = SimpleNamespace(enforce=False)
    monkeypatch.setattr(
        module, "hidden_from_core", lambda hass, config: {"light.hidden"}
    )
    loop_thread = threading.get_ident()
    original = module._project_overview
    threads = []

    def project(*arguments):
        threads.append(threading.get_ident())
        return original(*arguments)

    monkeypatch.setattr(module, "_project_overview", project)
    manager.hass.async_add_executor_job = asyncio.to_thread
    result = await engine.call(
        "ha_get_overview", {"detail_level": "full", "limit": 1}, CallContext("actor")
    )
    assert not result.is_error, result.data
    assert result.data["system_summary"]["total_entities"] == 2
    assert result.data["domain_stats"]["light"]["count"] == 1
    assert result.data["pagination"]["total_entity_results"] == 2
    assert result.data["pagination"]["has_more"] is True
    assert "light.hidden" not in str(result.data)
    assert result.data["notification_count"] == 1
    assert result.data["system_info"]["future_core_config"] == {"kept": False}
    assert threads and all(thread != loop_thread for thread in threads)
    assert result.data["settings_url"].endswith("/api/ha_mcp_tools/ui/boot")
    assert set(result.data["tool_discovery"]["pinned_tools"]) <= set(engine.tools)
    manager.backend.execute.assert_not_awaited()


async def test_native_overview_independent_fields_skip_all_unrelated_core_reads(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, manager, module = _setup(native, monkeypatch)
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.overview"
    )
    monkeypatch.setattr(
        owner, "_do_overview", lambda *args: pytest.fail("Unrelated Core scan")
    )
    manager.hass.states.async_all = lambda: pytest.fail("Unrelated state read")
    result = await engine.call(
        "ha_get_overview",
        {"fields": ["system_info"]},
        CallContext("actor", read_only=True),
    )
    assert not result.is_error, result.data
    assert result.data["system_info"]["version"] == "fixture"
    assert result.data["read_only_mode"] is True
    assert "system_summary" not in result.data
    manager.visibility._config.assert_not_awaited()


async def test_native_overview_can_apply_decluttering_through_the_real_engine_guard(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, manager, module = _setup(native, monkeypatch)
    visibility_module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_visibility"
    )
    manager.visibility = visibility_module.NativeVisibility(manager)
    manager.visibility._config = AsyncMock(
        return_value=VisibilityConfig(enabled=True, deny_entity_ids=["light.hidden"])
    )
    monkeypatch.setattr(
        module, "hidden_from_core", lambda hass, config: {"light.hidden"}
    )
    engine.policy_guard = lambda name, args, actor, writes: manager.visibility.before(
        name, args, actor
    )
    engine.result_filter = manager.visibility.after
    result = await engine.call("ha_get_overview", {}, CallContext("actor"))
    assert not result.is_error, result.data
    assert result.data["system_summary"]["total_entities"] == 2
    assert "light.hidden" not in str(result.data)


@pytest.mark.parametrize("change", ["visibility", "authorization"])
async def test_native_overview_does_not_return_a_projection_after_its_boundary_changes(
    native: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    engine, manager, module = _setup(native, monkeypatch)

    async def execute(function, *arguments):
        result = function(*arguments)
        if isinstance(function, partial):
            if change == "visibility":
                manager.visibility._config.return_value = object()
                monkeypatch.setattr(
                    module, "hidden_from_core", lambda hass, config: {"light.hidden"}
                )
            else:
                manager.backend.authorize = AsyncMock(
                    side_effect=PermissionError("revoked")
                )
        return result

    manager.hass.async_add_executor_job = execute
    result = await engine.call("ha_get_overview", {}, CallContext("actor"))
    assert result.is_error, result.data
    assert result.data["error"]["code"] == (
        "CONFIG_CONFLICT" if change == "visibility" else "AUTH_FORBIDDEN"
    )
    assert "system_summary" not in result.data


@pytest.mark.parametrize("slice_name", ["services", "states"])
async def test_native_overview_reports_a_core_read_failure_without_a_remote_fallback(
    native: Any, monkeypatch: pytest.MonkeyPatch, slice_name: str
) -> None:
    engine, manager, module = _setup(native, monkeypatch)
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.overview"
    )

    def fail(*args):
        raise RuntimeError("Core owner failed")

    monkeypatch.setattr(owner, "_overview_" + slice_name, fail)
    result = await engine.call("ha_get_overview", {}, CallContext("actor"))
    if slice_name == "states":
        assert result.is_error and result.data["error"]["code"] == "CORE_READ_FAILED", (
            result.data
        )
    else:
        assert not result.is_error and result.data["partial"] is True, result.data
        assert "services" in str(result.data["warnings"])
    manager.backend.execute.assert_not_awaited()


async def test_native_overview_worker_does_not_see_later_changes_to_live_core_config(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, module = _setup(native, monkeypatch)
    components = ["sensor"]
    manager.hass.config.as_dict = lambda: {"components": components}
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(function, *arguments):
        entered.set()
        await release.wait()
        return function(*arguments)

    manager.hass.async_add_executor_job = execute
    task = asyncio.create_task(
        OverviewTools(
            module.NativeOverview(manager, CallContext("actor"))
        ).ha_get_overview(detail_level="full", fields=["system_info"])
    )
    await entered.wait()
    components.append("added_after_capture")
    release.set()
    result = await task
    assert result["system_info"]["components"] == ["sensor"]


async def test_native_overview_retains_its_worker_until_repeated_cancellation_drains_it(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, module = _setup(native, monkeypatch)
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(function, *arguments):
        entered.set()
        await release.wait()
        result = function(*arguments)
        completed.set()
        return result

    manager.hass.async_add_executor_job = execute
    task = asyncio.create_task(
        OverviewTools(
            module.NativeOverview(manager, CallContext("actor"))
        ).ha_get_overview()
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done() and not completed.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert completed.is_set()
