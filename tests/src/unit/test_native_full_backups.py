"""Core backup lifecycle waits for owners and preserves safety before dispatch."""

import asyncio
import copy
import importlib
import sys
from enum import StrEnum
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


class State(StrEnum):
    IDLE = "idle"
    CREATE = "create_backup"


def row(identifier: str, date: str, **extra: Any) -> dict[str, Any]:
    return {
        "backup_id": identifier,
        "name": "Same name",
        "date": date,
        "agents": {"backup.local": {"size": 123, "protected": True}},
        "extra_metadata": {},
        "with_automatic_settings": False,
        **extra,
    }


@pytest.fixture
def backups(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    rows = {
        "old": row("old", "2026-01-01T00:00:00+00:00"),
        "newest": row(
            "newest", "2026-01-02T00:00:00+00:00", future_field={"complete": True}
        ),
    }
    flags = {
        "enable_snapshot_actions": True,
        "backup_read_only": False,
        "enable_snapshot_delete": True,
    }
    owner = SimpleNamespace(
        state=State.IDLE,
        backup_agents={"backup.local": SimpleNamespace(name="local")},
        config=SimpleNamespace(
            data=SimpleNamespace(
                create_backup=SimpleNamespace(password="fixture-protection")
            )
        ),
    )

    async def inventory() -> Any:
        return copy.deepcopy(rows), {}

    async def create(**kwargs: Any) -> Any:
        rows["created"] = row(
            "created",
            "2026-01-03T00:00:00+00:00",
            name=kwargs["name"],
            extra_metadata=kwargs["extra_metadata"],
        )
        return SimpleNamespace(backup_job_id="actual-core-job")

    async def delete(identifier: str) -> Any:
        rows.pop(identifier)
        return {}

    owner.async_get_backups = AsyncMock(side_effect=inventory)
    owner.async_create_backup = AsyncMock(side_effect=create)
    owner.async_delete_backup = AsyncMock(side_effect=delete)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.backup",
        SimpleNamespace(
            async_get_manager=lambda hass: hass.data["backup"], DATA_MANAGER="backup"
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.backup.manager",
        SimpleNamespace(BackupManagerState=State),
    )
    manager = SimpleNamespace(
        hass=SimpleNamespace(
            config=SimpleNamespace(components={"backup"}), data={"backup": owner}
        ),
        backend=SimpleNamespace(authorize=AsyncMock()),
        policy=SimpleNamespace(flag=lambda key: flags[key]),
        snapshots=SimpleNamespace(_value=lambda key: 0),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.full_backups"
    )
    service = module.NativeFullBackups(manager, CallContext("actor"))
    return SimpleNamespace(
        manager=manager, owner=owner, service=service, rows=rows, flags=flags
    )


async def test_backup_creation_matches_its_nonce_not_an_older_identical_name(
    backups: Any,
) -> None:
    result = await backups.service.create("Same name")
    assert (
        result["backup_id"] == "created"
        and result["backup_job_id"] == "actual-core-job"
    )
    sent = backups.owner.async_create_backup.call_args.kwargs
    assert not sent["include_database"] and not sent["include_all_addons"]
    assert sent["agent_ids"] == ["backup.local"]
    assert "fixture-protection" not in str(result)
    backups.owner.async_create_backup.assert_awaited_once()


async def test_native_backup_list_preserves_provider_fields_and_sorts_dates(
    backups: Any,
) -> None:
    result = await backups.service.list(1)
    assert result["count"] == 1 and result["total"] == 2
    assert result["backups"][0]["future_field"] == {"complete": True}


async def test_snapshot_controls_block_list_and_changes_before_core_io(
    backups: Any,
) -> None:
    backups.flags["enable_snapshot_actions"] = False
    with pytest.raises(ToolFailure):
        await backups.service.list(10)
    backups.owner.async_get_backups.assert_not_awaited()
    backups.flags["enable_snapshot_actions"] = True
    backups.flags["backup_read_only"] = True
    with pytest.raises(ToolFailure):
        await backups.service.create(None)
    backups.owner.async_create_backup.assert_not_awaited()


async def test_newest_backup_is_never_deleted_and_older_delete_uses_core_once(
    backups: Any,
) -> None:
    with pytest.raises(ToolFailure, match="newest"):
        await backups.service.delete("newest", True)
    backups.owner.async_delete_backup.assert_not_awaited()
    result = await backups.service.delete("old", True)
    assert result["success"] and set(backups.rows) == {"newest"}
    backups.owner.async_delete_backup.assert_awaited_once_with("old")


async def test_backup_newest_protection_rechecks_inventory_after_preparation(
    backups: Any,
) -> None:
    count = 0

    async def inventory() -> Any:
        nonlocal count
        count += 1
        if count == 2:
            backups.rows.pop("newest")
        return copy.deepcopy(backups.rows), {}

    backups.owner.async_get_backups.side_effect = inventory
    with pytest.raises(ToolFailure, match="newest"):
        await backups.service.delete("old", True)
    backups.owner.async_delete_backup.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", [RuntimeError("private details"), asyncio.CancelledError()]
)
async def test_core_backup_failure_after_effect_is_unknown_without_replay(
    backups: Any, failure: BaseException
) -> None:
    async def create(**kwargs: Any) -> Any:
        backups.rows["changed"] = row("changed", "2026-01-03T00:00:00Z")
        raise failure

    backups.owner.async_create_backup.side_effect = create
    with pytest.raises(ToolFailure) as caught:
        await backups.service.create(None)
    assert caught.value.status == "outcome_unknown" and "private details" not in str(
        caught.value
    )
    assert "changed" in backups.rows
    backups.owner.async_create_backup.assert_awaited_once()


async def test_cancellation_drains_core_finish_before_releasing_backup_lock(
    backups: Any,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    create = backups.owner.async_create_backup.side_effect

    async def held(**kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await create(**kwargs)

    backups.owner.async_create_backup.side_effect = held
    first = asyncio.create_task(backups.service.create(None))
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(backups.service.delete("old", True))
    try:
        for _ in range(8):
            await asyncio.sleep(0)
        assert not first.done()
        backups.owner.async_delete_backup.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            _ = await first
        _ = await second
