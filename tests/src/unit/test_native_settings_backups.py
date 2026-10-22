"""The administrator snapshot panel uses the same files and pin locks as tools."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.native_server_policy import NativePolicy  # noqa: E402
from custom_components.ha_mcp_tools.native_settings import NativeSettings  # noqa: E402
from custom_components.ha_mcp_tools.native_snapshots import NativeSnapshots  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolResult  # noqa: E402


@pytest.fixture(autouse=True)
def panel_actor(monkeypatch):
    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.native_settings_backups.settings_context",
        AsyncMock(return_value=CallContext("owner", client_id="settings:fixture")),
    )


def runtime(tmp_path):
    manager = SimpleNamespace(
        hass=SimpleNamespace(
            config=SimpleNamespace(path=lambda _subdir: str(tmp_path)),
            async_add_executor_job=lambda fn, *args: asyncio.ensure_future(
                asyncio.to_thread(fn, *args)
            ),
        ),
        backend=SimpleNamespace(authorize=AsyncMock()),
        policy=NativePolicy(flags={"backup_read_only": False}),
        is_running=True,
        refresh_policy=AsyncMock(),
    )
    ui = NativeSettings(manager)
    manager.settings_ui = ui
    manager.snapshots = NativeSnapshots(manager)
    manager.engine = Engine(manager.backend)
    manager.engine.enabled_tools = set()
    return ui, manager.snapshots._store()


def request(method="GET", **query):
    return SimpleNamespace(method=method, query=query)


async def test_panel_lists_and_reads_the_snapshot_written_by_tools(tmp_path) -> None:
    ui, store = runtime(tmp_path)
    snapshot = store.capture(
        "helper_input_boolean",
        "lamp",
        {"id": "lamp", "name": "Before edit"},
        "ha_config_set_helper",
    )
    listed = await ui.handle(request(), "api/settings/backups")
    assert listed.json_body["backups"][0]["name"] == snapshot.name
    viewed = await ui.handle(request(), f"api/settings/backups/{snapshot.name}")
    assert viewed.json_body["data"]["config"]["name"] == "Before edit"


async def test_panel_cannot_delete_a_snapshot_held_by_capture_or_restore(
    tmp_path,
) -> None:
    ui, store = runtime(tmp_path)
    snapshot = store.capture("helper_input_boolean", "lamp", {"id": "lamp"}, "test")
    store._protect_snapshot(snapshot.name)
    blocked = await ui.handle(
        request("DELETE"), f"api/settings/backups/{snapshot.name}"
    )
    assert blocked.status == 409 and snapshot.exists()
    store._unprotect_snapshot(snapshot.name)
    deleted = await ui.handle(
        request("DELETE"), f"api/settings/backups/{snapshot.name}"
    )
    assert deleted.json_body["deleted"] == [snapshot.name]
    assert not snapshot.exists()


async def test_bulk_delete_requires_and_honors_an_explicit_filter(tmp_path) -> None:
    ui, store = runtime(tmp_path)
    first = store.capture("helper_input_boolean", "first", {"id": "first"}, "test")
    second = store.capture("helper_input_boolean", "second", {"id": "second"}, "test")
    blocked = await ui.handle(request("DELETE"), "api/settings/backups")
    assert blocked.status == 400 and first.exists() and second.exists()
    deleted = await ui.handle(
        request("DELETE", entity_id="first"), "api/settings/backups"
    )
    assert deleted.json_body["deleted"] == [first.name]
    assert not first.exists() and second.exists()


async def test_panel_restore_uses_owned_engine_when_tool_is_hidden(tmp_path) -> None:
    ui, store = runtime(tmp_path)
    snapshot = store.capture("helper_input_boolean", "lamp", {"id": "lamp"}, "test")
    restored = {"apply_status": "applied", "safety_backup": "recovery.yaml"}
    ui.manager.snapshots.restore_snapshot = AsyncMock(return_value=restored)
    response = await ui.handle(
        request("POST"), f"api/settings/backups/{snapshot.name}/restore"
    )
    assert response.json_body == {"success": True, "data": restored}
    operation = next(iter(ui.manager.engine.operations.values()))
    assert operation["status"] == "confirmed"
    assert operation["user_id"] == "owner"
    assert operation["enforce_catalogue"] is False


async def test_panel_delete_respects_backup_read_only(tmp_path) -> None:
    ui, store = runtime(tmp_path)
    snapshot = store.capture("helper_input_boolean", "lamp", {"id": "lamp"}, "test")
    ui.manager.policy.flags["backup_read_only"] = True
    response = await ui.handle(
        request("DELETE"), f"api/settings/backups/{snapshot.name}"
    )
    assert response.status == 403 and snapshot.exists()


async def test_panel_approval_pending_does_not_start_restore(tmp_path) -> None:
    ui, _store = runtime(tmp_path)
    ui.manager.snapshots.restore_snapshot = AsyncMock()
    guard = AsyncMock(
        return_value=ToolResult(
            {"success": False, "approval_token": "pending"},
            is_error=True,
            status="not_started",
        )
    )
    ui.manager.engine.policy_guard = guard
    response = await ui.handle(
        request("POST"), "api/settings/backups/example.yaml/restore"
    )
    assert response.status == 409
    assert response.json_body["approval_token"] == "pending"
    ui.manager.snapshots.restore_snapshot.assert_not_awaited()
    assert guard.call_args.args[1] == {
        "scope": "edits",
        "action": "restore",
        "name": "example.yaml",
    }


async def test_panel_restore_preserves_applied_state_when_verification_fails(
    tmp_path,
) -> None:
    from ha_mcp.runtime.snapshot_payloads import BackupRestoreError

    ui, _store = runtime(tmp_path)
    ui.manager.snapshots.restore_snapshot = AsyncMock(
        side_effect=BackupRestoreError(
            "Verification failed",
            apply_status="applied",
            verification_status="unavailable",
            safety_backup="recovery.yaml",
        )
    )
    response = await ui.handle(
        request("POST"), "api/settings/backups/example.yaml/restore"
    )
    assert response.status == 409
    assert response.json_body["data"]["apply_status"] == "applied"
    assert response.json_body["data"]["safety_backup"] == "recovery.yaml"
    assert next(iter(ui.manager.engine.operations.values()))["status"] == "confirmed"


async def test_panel_diff_keeps_existing_yaml_response(tmp_path) -> None:
    ui, _store = runtime(tmp_path)
    ui.manager.snapshots.snapshot_comparison = AsyncMock(
        return_value=(
            {"entity_id": "lamp", "config": {"name": "Before"}},
            {"name": "After"},
        )
    )
    response = await ui.handle(request(), "api/settings/backups/example.yaml/diff")
    assert response.status == 200
    assert response.json_body["backup_present"] is True
    assert "-name: Before" in response.json_body["diff"]
    assert "+name: After" in response.json_body["diff"]
