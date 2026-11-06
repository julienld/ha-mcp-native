"""Native backup wiring carries the public selections before any side effect."""

import asyncio
import importlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.backup_tools import BackupFacade
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_full_backup_restore import restore as restore
from .test_native_full_backups import backups as backups


async def test_public_backup_facade_reaches_native_with_current_optional_arguments(
    backups: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.backups"
    )
    create = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(module.NativeFullBackups, "create", create)
    await BackupFacade(
        module.NativeBackups(backups.manager, CallContext("actor")).manage
    ).ha_manage_backup(
        scope="snapshot",
        action="create",
        include_homeassistant=True,
        include_database=True,
    )
    assert create.call_args.kwargs["parts"]["include_database"] is True


async def test_custom_database_selection_reaches_core(backups: Any) -> None:
    await backups.service.create(
        "History", parts={"include_homeassistant": True, "include_database": True}
    )
    assert (
        backups.owner.async_create_backup.call_args.kwargs["include_database"] is True
    )


@pytest.mark.parametrize(
    "parts",
    [
        {"include_homeassistant": False},
        {"include_database": True},
        {"include_homeassistant": True, "include_folders": ["share"]},
        {"include_homeassistant": True, "include_addons": ["some_app"]},
        {"include_homeassistant": True, "future_part": True},
    ],
)
async def test_invalid_custom_selection_has_no_backup_effect(
    backups: Any, parts: dict[str, Any]
) -> None:
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None, parts=parts)
    assert caught.value.status == "not_started"
    backups.owner.async_create_backup.assert_not_awaited()


async def test_restore_defaults_to_database_recorded_in_target_without_safety(
    restore: Any,
) -> None:
    restore.rows["old"]["database_included"] = True
    result = await restore.restore(restore.manager, CallContext("actor"), "old", None)
    assert result["restore_database"] is True
    assert result["safety_backup_id"] is None
    restore.owner.async_create_backup.assert_not_awaited()
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    assert (
        restore.owner.async_restore_backup.call_args.kwargs["restore_database"] is True
    )


async def test_restore_database_absent_from_archive_is_refused_before_safety(
    restore: Any,
) -> None:
    restore.rows["old"]["database_included"] = False
    with pytest.raises(ToolFailure) as caught:
        await restore.restore(
            restore.manager, CallContext("actor"), "old", True, safety_backup=True
        )
    assert caught.value.status == "not_started"
    restore.owner.async_create_backup.assert_not_awaited()
    restore.owner.async_restore_backup.assert_not_awaited()
    assert not restore.manager.system_lifecycle.pending


async def test_supervisor_database_contradiction_is_refused_instead_of_adjusted(
    restore: Any,
) -> None:
    restore.manager.hass.config.components.add("hassio")
    restore.owner.backup_agents = {"hassio.local": object()}
    restore.rows["old"]["agents"] = {"hassio.local": {"protected": True}}
    restore.rows["old"]["database_included"] = True
    with pytest.raises(ToolFailure) as caught:
        await restore.restore(
            restore.manager, CallContext("actor"), "old", False, safety_backup=True
        )
    assert caught.value.status == "not_started"
    restore.owner.async_create_backup.assert_not_awaited()
    assert not restore.manager.system_lifecycle.pending


@pytest.mark.parametrize(
    "selection",
    [
        {"restore_addons": ["missing_app"]},
        {"restore_folders": ["share"]},
    ],
)
async def test_non_supervisor_restore_parts_are_refused_before_safety(
    restore: Any, selection: dict[str, Any]
) -> None:
    with pytest.raises(ToolFailure) as caught:
        await restore.restore(
            restore.manager,
            CallContext("actor"),
            "old",
            False,
            safety_backup=True,
            **selection,
        )
    assert caught.value.status == "not_started"
    restore.owner.async_create_backup.assert_not_awaited()
    assert not restore.manager.system_lifecycle.pending


def supervise(backups: Any) -> None:
    backups.manager.hass.config.components.add("hassio")
    backups.owner.backup_agents = {"hassio.local": object()}
    for row in backups.rows.values():
        row["agents"] = {"hassio.local": {"protected": True, "size": 123}}


async def test_configured_backup_contents_and_automatic_agent_are_applied(
    backups: Any,
) -> None:
    config = backups.owner.config.data.create_backup
    config.agent_ids = ["backup.local"]
    config.include_database = True
    config.name = "Configured name"
    result = await backups.service.create(None)
    sent = backups.owner.async_create_backup.call_args.kwargs
    assert sent["include_database"] is True
    assert sent["name"] == "Configured name"
    assert sent["with_automatic_settings"] is True
    assert result["backup"]["database_included"] is True


async def test_explicit_custom_selection_uses_local_agent_and_ignores_automatic_contents(
    backups: Any,
) -> None:
    config = backups.owner.config.data.create_backup
    config.agent_ids = ["cloud.remote"]
    config.include_database = True
    await backups.service.create("Manual", parts={"include_homeassistant": True})
    sent = backups.owner.async_create_backup.call_args.kwargs
    assert sent["agent_ids"] == ["backup.local"]
    assert not sent["include_database"] and not sent["with_automatic_settings"]


async def test_supervisor_custom_parts_reach_core_after_installed_app_preflight(
    backups: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervise(backups)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.full_backups"
    )
    listing = AsyncMock(return_value={"addons": [{"slug": "my_app"}]})
    monkeypatch.setattr(module.SupervisorAccess, "installed", listing)
    await backups.service.create(
        "Selected",
        parts={
            "include_addons": ["my_app"],
            "include_folders": ["share"],
        },
    )
    sent = backups.owner.async_create_backup.call_args.kwargs
    assert sent["include_addons"] == ["my_app"] and sent["include_folders"] == ["share"]
    assert not sent["include_homeassistant"] and not sent["include_database"]
    assert not sent["include_all_addons"]


@pytest.mark.parametrize(
    "parts",
    [
        {"include_addons": ["absent"]},
        {
            "include_homeassistant": True,
            "include_all_addons": True,
            "include_addons": ["my_app"],
        },
        {"include_folders": ["invalid"]},
    ],
)
async def test_supervisor_selection_cannot_silently_omit_apps_or_invalid_folders(
    backups: Any, monkeypatch: pytest.MonkeyPatch, parts: dict[str, Any]
) -> None:
    supervise(backups)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.full_backups"
    )
    monkeypatch.setattr(
        module.SupervisorAccess,
        "installed",
        AsyncMock(return_value={"addons": [{"slug": "my_app"}]}),
    )
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None, parts=parts)
    assert caught.value.status == "not_started"
    backups.owner.async_create_backup.assert_not_awaited()


async def test_selected_restore_parts_are_pinned_and_backed_up_before_handoff(
    restore: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervise(restore)
    image = restore.rows["old"]
    image.update(database_included=True, addons=[{"slug": "my_app"}], folders=["share"])
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.full_backups"
    )
    monkeypatch.setattr(
        module.SupervisorAccess,
        "installed",
        AsyncMock(return_value={"addons": [{"slug": "my_app"}]}),
    )
    addons, folders = ["my_app"], ["share"]
    result = await restore.restore(
        restore.manager,
        CallContext("actor"),
        "old",
        None,
        safety_backup=True,
        restore_addons=addons,
        restore_folders=folders,
    )
    assert (
        result["operation_outcome"] == "scheduled"
        and result["restore_completed"] is False
    )
    sent = restore.owner.async_create_backup.call_args.kwargs
    assert sent["include_addons"] == addons and sent["include_folders"] == folders
    assert (
        sent["include_database"]
        and sent["include_homeassistant"]
        and not sent["include_all_addons"]
    )
    addons.append("mutated")
    folders.clear()
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    sent = restore.owner.async_restore_backup.call_args.kwargs
    assert sent["restore_addons"] == ["my_app"] and sent["restore_folders"] == ["share"]
    assert sent["restore_database"] is True


@pytest.mark.parametrize(
    "selection",
    [
        {"restore_addons": ["absent"]},
        {"restore_folders": ["share"]},
    ],
)
async def test_supervisor_missing_restore_parts_do_not_leave_a_safety_backup(
    restore: Any, selection: dict[str, Any]
) -> None:
    supervise(restore)
    with pytest.raises(ToolFailure) as caught:
        await restore.restore(
            restore.manager,
            CallContext("actor"),
            "old",
            None,
            safety_backup=True,
            **selection,
        )
    assert caught.value.status == "not_started"
    restore.owner.async_create_backup.assert_not_awaited()
    assert not restore.manager.system_lifecycle.pending


@pytest.mark.parametrize("password", [None, ""])
async def test_unprotected_restore_without_safety_does_not_require_a_default_password(
    restore: Any,
    password: str | None,
) -> None:
    restore.owner.config.data.create_backup.password = password
    restore.rows["old"]["agents"]["backup.local"]["protected"] = False
    await restore.restore(restore.manager, CallContext("actor"), "old", None)
    await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
    assert restore.owner.async_restore_backup.call_args.kwargs["password"] is None
    restore.owner.async_create_backup.assert_not_awaited()


async def test_preflight_failure_without_safety_is_never_reported_as_confirmed(
    restore: Any,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise ToolFailure("CONFIG_CONFLICT", "Stopped", status="not_started")

    restore.manager.system_lifecycle.schedule = refuse
    with pytest.raises(ToolFailure) as caught:
        await restore.restore(restore.manager, CallContext("actor"), "old", None)
    assert caught.value.status == "not_started"
    restore.owner.async_create_backup.assert_not_awaited()


@pytest.mark.parametrize(
    "selection",
    [
        {"scope": "edits", "action": "create", "include_homeassistant": True},
        {"scope": "snapshot", "action": "create", "restore_database": False},
        {
            "scope": "snapshot",
            "action": "delete",
            "backup_id": "old",
            "safety_backup": True,
        },
        {
            "scope": "edits",
            "action": "delete",
            "backup_name": "named.yaml",
            "domain": "automation",
        },
    ],
)
async def test_misplaced_public_options_are_refused_before_dispatch(
    backups: Any, monkeypatch: pytest.MonkeyPatch, selection: dict[str, Any]
) -> None:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.backups"
    )
    edits, create, delete = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(module.NativeEditBackups, "manage", edits)
    monkeypatch.setattr(module.NativeFullBackups, "create", create)
    monkeypatch.setattr(module.NativeFullBackups, "delete", delete)
    with pytest.raises(ToolFailure) as caught:
        await BackupFacade(
            module.NativeBackups(backups.manager, CallContext("actor")).manage
        ).ha_manage_backup(**selection)
    assert caught.value.status == "not_started"
    for effect in (edits, create, delete):
        effect.assert_not_awaited()


async def test_changed_automatic_contents_during_final_admission_block_creation(
    backups: Any,
) -> None:
    config = backups.owner.config.data.create_backup
    config.include_database = True

    async def authorize(_actor: Any) -> None:
        if backups.manager.backend.authorize.await_count == 2:
            config.include_database = False

    backups.manager.backend.authorize.side_effect = authorize
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None)
    assert caught.value.status == "not_started"
    backups.owner.async_create_backup.assert_not_awaited()
