"""Integration recovery changes only the enabled state of the same live entry."""

import copy
import sys
from enum import StrEnum
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.tool_api import CallContext

from .test_native_snapshot_restore import runtime as runtime


class Disabler(StrEnum):
    USER = "user"


class Entry:
    domain = "fixture"
    entry_id = "integration-entry"

    def __init__(self) -> None:
        self.disabled_by: Any = None
        self.title = "Current entry"

    @property
    def as_json_fragment(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "entry_id": self.entry_id,
            "title": self.title,
            "disabled_by": self.disabled_by,
        }


@pytest.fixture
def integration(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    manager, snapshots, store, _old_path, _rows, _calls = runtime
    entry = Entry()
    entries = {entry.entry_id: entry}

    async def set_disabled(identifier: str, value: Any) -> bool:
        entries[identifier].disabled_by = value
        return True

    owner = SimpleNamespace(
        async_get_entry=entries.get,
        async_set_disabled_by=AsyncMock(side_effect=set_disabled),
    )
    manager.hass.config_entries = owner
    manager.hass.data = {
        "websocket_api": {"config_entries/disable": (None, lambda value: value)}
    }
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(
            ConfigEntryDisabler=Disabler,
            OperationNotAllowed=type("OperationNotAllowed", (Exception,), {}),
            UnknownEntry=type("UnknownEntry", (Exception,), {}),
        ),
    )
    saved = {
        **entry.as_json_fragment,
        "title": "Historical informational title",
        "disabled_by": "user",
    }
    path = store.capture("integration", entry.entry_id, saved, "fixture")
    return SimpleNamespace(
        manager=manager,
        snapshots=snapshots,
        store=store,
        entry=entry,
        entries=entries,
        owner=owner,
        path=path,
    )


async def test_integration_snapshot_restores_enabled_state_but_not_credentials_or_metadata(
    integration: Any,
) -> None:
    before = copy.deepcopy(integration.entry.as_json_fragment)
    result = await integration.snapshots.restore_snapshot(
        integration.path.name, CallContext("actor")
    )
    assert result["apply_status"] == "applied"
    assert integration.entry.disabled_by == "user"
    assert integration.entry.title == "Current entry"
    safety = integration.store.read_snapshot(result["safety_backup"])
    assert safety["config"] == before
    assert "data" not in safety["config"] and "options" not in safety["config"]
    integration.owner.async_set_disabled_by.assert_awaited_once()


async def test_deleted_integration_is_not_recreated_from_public_metadata(
    integration: Any,
) -> None:
    integration.entries.clear()
    with pytest.raises(BackupRestoreError) as caught:
        await integration.snapshots.restore_snapshot(
            integration.path.name, CallContext("actor")
        )
    assert caught.value.outcome["reason"] == "entry_deleted"
    integration.owner.async_set_disabled_by.assert_not_awaited()


async def test_replaced_entry_during_safety_or_final_admission_never_receives_old_restore(
    integration: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = integration.store.capture

    def replace_entry(*args: Any, **kwargs: Any) -> Any:
        path = original(*args, **kwargs)
        integration.entries[integration.entry.entry_id] = Entry()
        return path

    monkeypatch.setattr(integration.store, "capture", replace_entry)
    with pytest.raises(BackupRestoreError) as caught:
        await integration.snapshots.restore_snapshot(
            integration.path.name, CallContext("actor")
        )
    assert caught.value.outcome["apply_status"] == "not_applied"
    integration.owner.async_set_disabled_by.assert_not_awaited()


async def test_revocation_after_core_changed_integration_is_not_reported_as_unstarted(
    integration: Any,
) -> None:
    original = integration.owner.async_set_disabled_by.side_effect

    async def revoke(identifier: str, disabled: Any) -> Any:
        result = await original(identifier, disabled)
        integration.manager.backend.authorize.side_effect = PermissionError("revoked")
        return result

    integration.owner.async_set_disabled_by.side_effect = revoke
    with pytest.raises(BackupRestoreError) as caught:
        await integration.snapshots.restore_snapshot(
            integration.path.name, CallContext("actor")
        )
    assert caught.value.outcome["apply_status"] == "unknown"
    assert integration.entry.disabled_by == "user"
    integration.owner.async_set_disabled_by.assert_awaited_once()


async def test_integration_restore_rechecks_owner_after_the_last_admission_await(
    integration: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_operations.integration_state import (
        NativeIntegrationState,
    )

    original = NativeIntegrationState._admit

    async def replace_after_admission(self: Any, actor: Any) -> None:
        await original(self, actor)
        integration.entries[integration.entry.entry_id] = Entry()

    monkeypatch.setattr(NativeIntegrationState, "_admit", replace_after_admission)
    with pytest.raises(BackupRestoreError) as caught:
        await integration.snapshots.restore_snapshot(
            integration.path.name, CallContext("actor")
        )
    assert caught.value.outcome["apply_status"] == "not_applied"
    integration.owner.async_set_disabled_by.assert_not_awaited()
