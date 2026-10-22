"""Dashboard public calls use one contract and compare their actual Core owners."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext
from ha_mcp.utils.config_hash import compute_config_hash

from .test_native_dashboard_storage import LovelaceStorage
from .test_native_dashboard_storage import dashboards as dashboards
from .test_native_engine_backend import native as native


@pytest.fixture
def service(dashboards, monkeypatch):
    from custom_components.ha_mcp_tools.native_dashboard_tools import (
        build_dashboard_tools,
    )
    from custom_components.ha_mcp_tools.native_operations.dashboard_advice import (
        DashboardAdvice,
    )
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy

    storage, owner, collection, active, _module = dashboards
    storage.hass.async_add_executor_job = lambda function, *args: asyncio.ensure_future(
        asyncio.to_thread(function, *args)
    )
    manager = SimpleNamespace(
        hass=storage.hass,
        backend=SimpleNamespace(authorize=storage.authorize),
        snapshots=SimpleNamespace(dashboard_backup=AsyncMock(return_value=None)),
        policy=NativePolicy(
            flags={"enable_mandatory_bps": False, "enable_dashboard_screenshot": False}
        ),
    )
    monkeypatch.setattr(DashboardAdvice, "warnings", AsyncMock(return_value=[]))
    monkeypatch.setattr(DashboardAdvice, "attach_skill", AsyncMock())
    engine = Engine(manager.backend, build_dashboard_tools(manager))
    return manager, engine, owner, collection, active


async def test_dashboard_public_metadata_body_patch_transform_and_delete(service):
    manager, engine, owner, _collection, _active = service
    actor = CallContext("owner")
    listed = await engine.call("ha_config_get_dashboard", {"list_only": True}, actor)
    assert listed.data["dashboards"][0]["future_field"] is False
    original = copy.deepcopy(owner._data["config"])
    first = await engine.call("ha_config_get_dashboard", {"url_path": "my_dash"}, actor)
    assert first.data["resolved_from"] == "my_dash"
    changed = await engine.call(
        "ha_config_set_dashboard",
        {
            "url_path": "my-dash",
            "patch": [
                {
                    "op": "add",
                    "path": "/views/-",
                    "value": {"path": "main", "title": "Main", "cards": []},
                }
            ],
            "config_hash": first.data["config_hash"],
        },
        actor,
    )
    assert not changed.is_error and changed.data["action"] == "patch", changed.data
    captured = manager.snapshots.dashboard_backup.await_args.args[0]
    assert captured["current_config"]["config"] == original
    assert captured["current_config"]["dashboard"]["future_field"] is False
    transformed = await engine.call(
        "ha_config_set_dashboard",
        {
            "url_path": "my-dash",
            "python_transform": "config['views'][0]['title'] = 'New'",
            "config_hash": changed.data["config_hash"],
        },
        actor,
    )
    assert not transformed.is_error and transformed.data[
        "config_hash"
    ] == compute_config_hash(owner._data["config"]), transformed.data
    assert (
        manager.snapshots.dashboard_backup.await_args.args[0]["current_config"][
            "config"
        ]["views"][0]["title"]
        == "Main"
    )
    scoped = await engine.call(
        "ha_config_get_dashboard", {"url_path": "my-dash", "view_path": "main"}, actor
    )
    assert scoped.data["view"]["title"] == "New" and "config" not in scoped.data
    deleted = await engine.call(
        "ha_config_delete_dashboard", {"url_path": "my-dash"}, actor
    )
    assert not deleted.is_error and deleted.data["resolved_id"] == "my_dash", (
        deleted.data
    )


async def test_dashboard_hash_conflict_precedes_metadata_and_capture(service):
    manager, engine, owner, *_ = service
    result = await engine.call(
        "ha_config_set_dashboard",
        {
            "url_path": "my-dash",
            "config": {"views": []},
            "title": "Should not write",
            "config_hash": "stale",
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started"
    assert owner.config["title"] == "Before" and not owner.calls
    manager.snapshots.dashboard_backup.assert_not_awaited()


async def test_dashboard_change_during_snapshot_does_not_overwrite(service):
    manager, engine, owner, *_ = service

    async def backup(payload, actor):
        owner._data["config"] = {"views": [{"title": "Concurrent"}]}

    manager.snapshots.dashboard_backup.side_effect = backup
    result = await engine.call(
        "ha_config_set_dashboard",
        {"url_path": "my-dash", "config": {"views": []}},
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started"
    assert (
        owner._data["config"]["views"][0]["title"] == "Concurrent" and not owner.calls
    )


async def test_dashboard_metadata_cannot_refresh_the_body_before_image(service):
    manager, engine, owner, collection, _active = service

    async def concurrent():
        owner._data["config"] = {"views": [{"title": "Concurrent"}]}

    collection.notify = concurrent
    result = await engine.call(
        "ha_config_set_dashboard",
        {
            "url_path": "my-dash",
            "title": "Metadata applied",
            "config": {"views": [{"title": "Desired"}]},
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert (
        result.data["write_committed"] is True and result.data["config_applied"] is None
    )
    assert owner.config["title"] == "Metadata applied" and not owner.calls
    assert owner._data["config"]["views"][0]["title"] == "Concurrent"


@pytest.mark.parametrize("title", [None, ""])
async def test_dashboard_create_has_no_false_before_image_and_preserves_core_id(
    service,
    title,
):
    manager, engine, _owner, collection, _active = service
    result = await engine.call(
        "ha_config_set_dashboard",
        {"url_path": "new-dash", "config": {"views": []}, "title": title},
        CallContext("owner"),
    )
    assert not result.is_error and result.data["dashboard_created"] is True, result.data
    assert result.data["dashboard_id"] == "new_dash" and "new_dash" in collection.data
    assert collection.data["new_dash"]["title"] == "New Dash"
    manager.snapshots.dashboard_backup.assert_not_awaited()


async def test_dashboard_search_reuses_pure_card_discovery_and_keeps_hash(service):
    _manager, engine, owner, *_ = service
    owner._data["config"] = {
        "views": [{"cards": [{"type": "tile", "entity": "light.kitchen"}]}]
    }
    result = await engine.call(
        "ha_config_get_dashboard",
        {"query": "light.kitchen", "include_config": True},
        CallContext("owner", read_only=True),
    )
    assert not result.is_error and result.data["match_count"] == 1, result.data
    match = result.data["matches"][0]
    assert match["config_hash"] == compute_config_hash(owner._data["config"])
    assert match["card_config"]["entity"] == "light.kitchen"


async def test_dashboard_completed_save_with_failed_readback_keeps_success_and_unknown_hash(
    service, monkeypatch
):
    _manager, engine, owner, *_ = service
    read = owner.async_load

    async def broken_after_save(force):
        if owner.calls:
            raise ValueError("readback unavailable")
        return await read(force)

    monkeypatch.setattr(owner, "async_load", broken_after_save)
    result = await engine.call(
        "ha_config_set_dashboard",
        {"url_path": "my-dash", "config": {"views": []}},
        CallContext("owner"),
    )
    assert not result.is_error and result.data["write_committed"] is True, result.data
    assert (
        result.data["config_hash"] is None
        and result.data["post_write_verified"] is False
    )
    assert result.data["warnings"] and len(owner.calls) == 1


async def test_dashboard_screenshot_option_respects_the_beta_master(
    service, monkeypatch
):
    manager, engine, _owner, *_ = service
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "false")
    monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "true")
    result = await engine.call(
        "ha_config_get_dashboard",
        {"url_path": "my-dash", "include_screenshot": True},
        CallContext("owner"),
    )
    assert not result.is_error and "config" in result.data
    assert any(
        "screenshot mode is disabled" in warning for warning in result.data["warnings"]
    )


async def test_dashboard_search_preserves_readable_results_when_one_owner_fails(
    service,
):
    _manager, engine, owner, collection, _active = service
    owner.async_load = AsyncMock(side_effect=ValueError("unavailable config"))
    collection.dashboards["other-dash"] = LovelaceStorage(
        {"id": "other_dash", "url_path": "other-dash", "title": "Other"},
        {"views": [{"cards": [{"type": "tile", "entity": "light.visible"}]}]},
    )
    result = await engine.call(
        "ha_config_get_dashboard", {"query": "light.visible"}, CallContext("owner")
    )
    assert not result.is_error and result.data["match_count"] == 1, result.data
    assert any(
        "my-dash" in warning and "not" in warning for warning in result.data["warnings"]
    )


async def test_dashboard_divergent_readback_does_not_confirm_another_writers_hash(
    service,
):
    _manager, engine, owner, *_ = service
    owner.release = asyncio.Event()
    first = asyncio.create_task(
        engine.call(
            "ha_config_set_dashboard",
            {"url_path": "my-dash", "config": {"views": [{"title": "Requested"}]}},
            CallContext("owner"),
        )
    )
    second = None
    try:
        await asyncio.wait_for(owner.loaded.wait(), 2)
        second = asyncio.create_task(
            owner.async_save({"views": [{"title": "Other writer"}]})
        )
        await asyncio.sleep(0)
        owner.release.set()
        result = await first
    finally:
        owner.release.set()
        await asyncio.gather(
            *([first, second] if second is not None else [first]),
            return_exceptions=True,
        )
    assert not result.is_error and result.data["write_committed"] is True
    assert (
        result.data["post_write_verified"] is False
        and result.data["config_hash"] is None
    )
    assert (
        owner._data["config"]["views"][0]["title"] == "Other writer"
        and len(owner.calls) == 2
    )


async def test_dashboard_noop_patch_rechecks_after_executor_before_returning_a_hash(
    service,
):
    manager, engine, owner, *_ = service
    executor = manager.hass.async_add_executor_job

    def changed(function, *args):
        future = executor(function, *args)
        if (
            getattr(getattr(function, "func", None), "__name__", None)
            == "apply_dashboard_patch"
        ):
            owner._data["config"] = {"views": [{"title": "Concurrent"}]}
        return future

    manager.hass.async_add_executor_job = changed
    result = await engine.call(
        "ha_config_set_dashboard",
        {
            "url_path": "my-dash",
            "patch": [],
            "config_hash": compute_config_hash({"views": []}),
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert not owner.calls
    manager.snapshots.dashboard_backup.assert_not_awaited()
