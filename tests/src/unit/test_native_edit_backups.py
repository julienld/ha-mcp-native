"""The backup tool uses the existing snapshot files and restore coordinator."""

import copy
import importlib
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def edits(runtime: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    manager, snapshots, store, path, rows, calls = runtime
    manager.hass.config.path = lambda *parts: (
        str(tmp_path / parts[0])
        if parts == (".ha_mcp_tools_backups",)
        else str(tmp_path)
    )
    registry = sys.modules["homeassistant.helpers.entity_registry"].async_get(
        manager.hass
    )
    registry.async_get_entity_id = lambda *_args: "input_boolean.renamed"
    manager.visibility = SimpleNamespace(before=AsyncMock())
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_backup_tools"
    )
    engine = Engine(manager.backend, [module.backup_definition(manager)])
    return SimpleNamespace(
        manager=manager,
        snapshots=snapshots,
        store=store,
        path=path,
        rows=rows,
        calls=calls,
        engine=engine,
    )


async def call(edits: Any, action: str, **kwargs: Any) -> Any:
    return await edits.engine.call(
        "ha_manage_backup",
        {"scope": "edits", "action": action, **kwargs},
        CallContext("actor"),
    )


async def test_edit_list_view_diff_restore_and_delete_use_the_same_real_store(
    edits: Any,
) -> None:
    listing = await call(edits, "list", domain="helper_input_boolean")
    assert listing.data["data"]["count"] == 1, listing.data
    name = edits.path.name
    viewed = await call(edits, "view", backup_name=name)
    assert viewed.data["data"]["config"]["name"] == "Snapshot", viewed.data
    diff = await call(edits, "diff", backup_name=name)
    assert not diff.is_error and not diff.data["data"]["unchanged"], diff.data
    restored = await call(edits, "restore", backup_name=name)
    assert not restored.is_error, restored.data
    assert edits.rows["original"]["name"] == "Snapshot"
    assert restored.data["data"]["safety_backup"]
    deleted = await call(edits, "delete", backup_name=name)
    assert deleted.data["data"]["deleted"] == [name]
    assert not edits.path.exists()


async def test_explicit_edit_capture_bypasses_automatic_toggle_and_returns_stored_identity(
    edits: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_operations.backup_capture import (
        NativeCaptureSources,
    )

    edits.manager.policy = replace(
        edits.manager.policy,
        flags={**edits.manager.policy.flags, "enable_auto_backup": False},
    )
    read = AsyncMock(
        return_value=("canonical", {"id": "canonical", "name": "Captured"})
    )
    monkeypatch.setattr(NativeCaptureSources, "read", read)
    result = await call(
        edits, "create", domain="helper_input_boolean", entity_id="input_boolean.alias"
    )
    assert not result.is_error, result.data
    record = result.data["data"]
    assert record["entity_id"] == "canonical" and record["size"] > 0
    assert (
        edits.store.read_snapshot(record["backup_name"])["config"]["name"] == "Captured"
    )
    read.assert_awaited_once_with("helper_input_boolean", "input_boolean.alias")


async def test_backup_read_only_keeps_edits_reads_but_blocks_explicit_capture(
    edits: Any,
) -> None:
    edits.manager.policy = replace(
        edits.manager.policy,
        flags={
            **edits.manager.policy.flags,
            "backup_read_only": True,
            "enable_snapshot_actions": False,
        },
    )
    listed = await call(edits, "list", domain="helper_input_boolean")
    assert not listed.is_error
    failed = await call(
        edits, "create", domain="helper_input_boolean", entity_id="missing"
    )
    assert failed.is_error and failed.status == "not_started", failed


async def test_invalid_scope_action_is_rejected_before_any_backup_effect(
    edits: Any,
) -> None:
    result = await edits.engine.call(
        "ha_manage_backup",
        {"scope": "snapshot", "action": "view"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result
    assert result.data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_changed_visibility_after_safety_blocks_restore_before_actual_write(
    edits: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = edits.store.capture
    changed = False

    def safety(*args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        result = capture(*args, **kwargs)
        changed = True
        return result

    async def visibility(_name: str, arguments: dict[str, Any], _actor: Any) -> None:
        assert arguments["snapshot"]["entity_id"] == "input_boolean.old"
        assert "input_boolean.renamed" in arguments["entity_ids"]
        if changed:
            raise ToolFailure(
                "VISIBILITY_ENFORCED", "Target is hidden", status="not_started"
            )

    monkeypatch.setattr(edits.store, "capture", safety)
    edits.manager.visibility.before.side_effect = visibility
    before = copy.deepcopy(edits.rows)
    result = await call(edits, "restore", backup_name=edits.path.name)
    assert result.is_error and result.status == "not_started", result
    assert edits.rows == before
    assert not any(
        operation == "helpers.storage.write" for operation, _args in edits.calls
    )


async def test_historical_bak_is_read_through_the_component_file_owner(
    edits: Any, tmp_path: Any
) -> None:
    directory = tmp_path / ".ha_mcp_tools_backups"
    directory.mkdir()
    filename = "configuration.yaml.20260101_010203.bak"
    (directory / filename).write_text("automation: []\n", encoding="utf-8")
    result = await call(edits, "view", backup_name="legacy:" + filename)
    assert (
        not result.is_error and result.data["data"]["content"] == "automation: []\n"
    ), result.data
    refused = await call(edits, "view", backup_name="legacy:../configuration.yaml.bak")
    assert refused.is_error
