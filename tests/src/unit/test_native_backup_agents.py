"""Configured Core destinations and remote archive copies survive native dispatch."""

import asyncio
import copy
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_full_backup_restore import restore as restore
from .test_native_full_backups import backups as backups


@pytest.mark.parametrize(
    "locations", [["cloud.remote"], ["cloud.remote", "backup.local"]]
)
async def test_automatic_backup_confirms_every_configured_destination(
    backups: Any, locations: list[str]
) -> None:
    backups.owner.backup_agents["cloud.remote"] = object()
    backups.owner.config.data.create_backup.agent_ids = locations
    result = await backups.service.create(None)
    sent = backups.owner.async_create_backup.call_args.kwargs
    assert sent["agent_ids"] == locations and sent["with_automatic_settings"]
    assert set(result["backup"]["agents"]) == set(locations)
    assert result["backup_job_id"] == "actual-core-job"
    assert "fixture-protection" not in str(result)


async def test_unavailable_configured_destination_refuses_before_any_archive(
    backups: Any,
) -> None:
    backups.owner.config.data.create_backup.agent_ids = [
        "backup.local",
        "missing.remote",
    ]
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None)
    assert caught.value.status == "not_started"
    backups.owner.async_create_backup.assert_not_awaited()


async def test_remote_upload_missing_from_readback_is_unknown_without_replay(
    backups: Any,
) -> None:
    backups.owner.backup_agents["cloud.remote"] = object()
    backups.owner.config.data.create_backup.agent_ids = ["backup.local", "cloud.remote"]

    async def inventory() -> Any:
        rows = copy.deepcopy(backups.rows)
        if "created" in rows:
            rows["created"]["agents"].pop("cloud.remote", None)
        return rows, {}

    backups.owner.async_get_backups.side_effect = inventory
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None)
    assert caught.value.status == "outcome_unknown"
    backups.owner.async_create_backup.assert_awaited_once()


async def test_uploaded_copy_must_confirm_requested_protection(backups: Any) -> None:
    async def inventory() -> Any:
        rows = copy.deepcopy(backups.rows)
        if "created" in rows:
            rows["created"]["agents"]["backup.local"]["protected"] = False
        return rows, {}

    backups.owner.async_get_backups.side_effect = inventory
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None)
    assert caught.value.status == "outcome_unknown"
    backups.owner.async_create_backup.assert_awaited_once()


@pytest.mark.parametrize("change", ["selection", "owner", "protection"])
async def test_destination_change_during_admission_blocks_all_uploads(
    backups: Any, change: str
) -> None:
    backups.owner.backup_agents["cloud.remote"] = object()
    backups.owner.config.data.create_backup.agent_ids = ["cloud.remote"]
    settings = SimpleNamespace(protected=True)
    backups.owner.config.data.agents = {"cloud.remote": settings}

    async def authorize(_actor: Any) -> None:
        if backups.manager.backend.authorize.await_count != 2:
            return
        if change == "selection":
            backups.owner.config.data.create_backup.agent_ids = ["backup.local"]
        elif change == "owner":
            backups.owner.backup_agents["cloud.remote"] = object()
        else:
            settings.protected = False

    backups.manager.backend.authorize.side_effect = authorize
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None)
    assert caught.value.status == "not_started"
    backups.owner.async_create_backup.assert_not_awaited()


async def test_remote_only_restore_uses_core_agent_without_local_storage(
    restore: Any,
) -> None:
    restore.owner.backup_agents = {"cloud.remote": object()}
    restore.rows["old"]["agents"] = {"cloud.remote": {"protected": True, "size": 123}}
    result = await restore.restore(restore.manager, CallContext("actor"), "old", None)
    assert result["operation_outcome"] == "scheduled"
    assert result["restore_completed"] is False
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    assert (
        restore.owner.async_restore_backup.call_args.kwargs["agent_id"]
        == "cloud.remote"
    )
    restore.owner.async_create_backup.assert_not_awaited()


async def test_restore_protection_comes_from_selected_copy(restore: Any) -> None:
    restore.owner.backup_agents["cloud.remote"] = object()
    restore.owner.config.data.create_backup.password = None
    restore.rows["old"]["agents"] = {
        "backup.local": {"protected": False},
        "cloud.remote": {"protected": True},
    }
    await restore.restore(restore.manager, CallContext("actor"), "old", None)
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    sent = restore.owner.async_restore_backup.call_args.kwargs
    assert sent["agent_id"] == "backup.local" and sent["password"] is None


async def test_scheduled_restore_never_switches_a_changed_provider(
    restore: Any,
) -> None:
    restore.owner.backup_agents = {"cloud.first": object(), "cloud.second": object()}
    restore.rows["old"]["agents"] = {
        "cloud.first": {"protected": True},
        "cloud.second": {"protected": True},
    }
    await restore.restore(restore.manager, CallContext("actor"), "old", None)
    restore.owner.backup_agents["cloud.first"] = object()
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    restore.owner.async_restore_backup.assert_not_awaited()


async def test_manual_backup_uses_supervisor_default_mount(
    backups: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups.manager.hass.config.components.add("hassio")
    backups.owner.backup_agents = {"hassio.nas": object()}
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.full_backups"
    )
    monkeypatch.setattr(
        module.SupervisorAccess,
        "_read",
        AsyncMock(return_value={"default_backup_mount": "nas"}),
    )
    result = await backups.service.create(
        "Safety", parts={"include_homeassistant": True}
    )
    assert result["success"]
    sent = backups.owner.async_create_backup.call_args.kwargs
    assert sent["agent_ids"] == ["hassio.nas"] and not sent["with_automatic_settings"]
