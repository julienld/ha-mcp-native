"""Full restore pins one archive, creates safety, then uses the existing handoff."""

import asyncio
import copy
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_full_backups import backups as backups


@pytest.fixture
def restore(backups: Any) -> Any:
    async def get(identifier: str) -> Any:
        return copy.deepcopy(backups.rows.get(identifier)), {}

    backups.owner.async_get_backup = AsyncMock(side_effect=get)
    backups.owner.async_restore_backup = AsyncMock()
    backups.owner._reader_writer = object()
    backups.manager.visibility = SimpleNamespace(before=AsyncMock())
    backups.manager.refresh_policy = AsyncMock()
    backups.manager.accepting = True
    backups.manager.policy.security_policy = None
    backups.manager.hass.async_create_task = lambda operation, name: (
        asyncio.create_task(operation, name=name)
    )
    lifecycle = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.system_lifecycle"
    )
    backups.manager.system_lifecycle = lifecycle.NativeSystemLifecycle(
        backups.manager, reply_delay=0
    )
    backups.restore = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.full_backup_restore"
    ).schedule_full_restore
    return backups


async def test_restore_waits_for_real_safety_then_schedules_existing_core_handoff(
    restore: Any,
) -> None:
    result = await restore.restore(restore.manager, CallContext("actor"), "old", False)
    assert (
        result["restore_completed"] is False and result["safety_backup_id"] == "created"
    )
    assert result["operation_outcome"] == "scheduled"
    assert restore.owner.async_create_backup.call_args.kwargs["include_database"]
    restore.owner.async_restore_backup.assert_not_awaited()
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    await asyncio.sleep(0)
    restore.owner.async_restore_backup.assert_awaited_once_with(
        "old",
        agent_id="backup.local",
        password="fixture-protection",
        restore_addons=[],
        restore_folders=[],
        restore_homeassistant=True,
        restore_database=False,
    )


async def test_restore_target_change_during_safety_does_not_restore_new_archive(
    restore: Any,
) -> None:
    create = restore.owner.async_create_backup.side_effect

    async def replace_target(**kwargs: Any) -> Any:
        result = await create(**kwargs)
        restore.rows["old"]["extra_metadata"] = {"different_archive": True}
        return result

    restore.owner.async_create_backup.side_effect = replace_target
    with pytest.raises(ToolFailure) as caught:
        await restore.restore(restore.manager, CallContext("actor"), "old", False)
    assert caught.value.code == "CONFIG_CONFLICT"
    assert caught.value.data["safety_backup_id"] == "created"
    assert caught.value.data["restore_requested"] is False
    restore.owner.async_restore_backup.assert_not_awaited()
    assert not restore.manager.system_lifecycle.pending


async def test_read_only_after_reply_stops_backup_restore_before_core_handoff(
    restore: Any,
) -> None:
    await restore.restore(restore.manager, CallContext("actor"), "old", False)
    restore.flags["backup_read_only"] = True
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    restore.owner.async_restore_backup.assert_not_awaited()


async def test_missing_backup_password_blocks_safety_and_destructive_restore(
    restore: Any,
) -> None:
    restore.owner.config.data.create_backup.password = None
    with pytest.raises(ToolFailure):
        await restore.restore(restore.manager, CallContext("actor"), "old", False)
    restore.owner.async_create_backup.assert_not_awaited()
    restore.owner.async_restore_backup.assert_not_awaited()
