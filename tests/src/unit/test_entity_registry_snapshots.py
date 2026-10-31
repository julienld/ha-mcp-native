"""Entity editing history stores registry settings, including after a rename."""

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.tools.tools_entities import EntityTools


def _expose_rows(rows: dict, payload: dict) -> dict:
    for entity_id in payload["entity_ids"]:
        for assistant in payload["assistants"]:
            rows[entity_id]["options"][assistant] = {
                "should_expose": payload["should_expose"]
            }
    return {"success": True}


@pytest.fixture
def registry_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    rows = {
        entity_id: {
            "id": identity,
            "entity_id": entity_id,
            "created_at": 1.0,
            "platform": "example",
            "unique_id": identity,
            "config_entry_id": "entry",
            "name": "Before",
            "icon": "mdi:lightbulb",
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "device_class": None,
            "aliases": [None, "Old alias"],
            "labels": [],
            "categories": {},
            "options": {"sensor": {"display_precision": 1}},
        }
        for entity_id, identity in (
            ("sensor.first", "uuid-first"),
            ("sensor.second", "uuid-second"),
        )
    }
    client = MagicMock()
    client._request = AsyncMock(
        return_value={"state": "42", "attributes": {"unrelated": True}}
    )
    client.get_entity_registry_entry = AsyncMock(
        side_effect=lambda entity_id: copy.deepcopy(rows.get(entity_id))
    )
    writes = []

    async def command(payload: dict) -> dict:
        payload = copy.deepcopy(payload)
        kind = payload.pop("type")
        if kind == "config/entity_registry/list":
            return {"success": True, "result": copy.deepcopy(list(rows.values()))}
        if kind == "homeassistant/expose_entity":
            return _expose_rows(rows, payload)
        if kind == "config/entity_registry/get_entries":
            return {
                "success": True,
                "result": {
                    entity_id: copy.deepcopy(rows[entity_id])
                    for entity_id in payload["entity_ids"]
                },
            }
        entity_id = payload.pop("entity_id")
        if kind == "config/entity_registry/get":
            return (
                {"success": True, "result": copy.deepcopy(rows[entity_id])}
                if entity_id in rows
                else {
                    "success": False,
                    "error_code": "not_found",
                    "error": {"message": "Entity not found"},
                }
            )
        writes.append((kind, entity_id, copy.deepcopy(payload)))
        if kind == "config/entity_registry/remove":
            rows.pop(entity_id)
            return {"success": True}
        row = rows[entity_id]
        if "options_domain" in payload:
            domain = payload.pop("options_domain")
            options = payload.pop("options")
            if options is None:
                row["options"].pop(domain, None)
            else:
                row["options"][domain] = options
        renamed = payload.pop("new_entity_id", None)
        row.update(payload)
        if renamed:
            rows.pop(entity_id)
            row["entity_id"] = renamed
            rows[renamed] = row
        return {"success": True, "result": {"entity_entry": copy.deepcopy(row)}}

    client.send_websocket_message = AsyncMock(side_effect=command)
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=10,
        auto_backup_dir=str(tmp_path),
    )
    manager = bm.BackupManager(settings, client)
    bm.register_default_handlers(manager, client)
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )
    monkeypatch.setattr("ha_mcp.config.get_global_settings", lambda: settings)
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_backup_manager", lambda *_: manager
    )
    monkeypatch.setattr(bm, "get_backup_manager", lambda *_: manager)
    return EntityTools(client), manager, client, rows, writes


@pytest.mark.parametrize("action", ["set", "remove", "bulk_remove", "bulk_expose"])
async def test_entity_tools_capture_actual_registry_fields_under_durable_ids(
    registry_history: tuple, action: str
) -> None:
    tools, manager, client, rows, _ = registry_history
    before = copy.deepcopy(rows)
    if action == "set":
        await tools.ha_set_entity(entity_id="sensor.first", name="After")
    elif action == "remove":
        await tools.ha_remove_entity(entity_id="sensor.first")
    elif action == "bulk_remove":
        await tools.ha_remove_entity(entity_id=["sensor.first", "sensor.second"])
    else:
        await tools.ha_set_entity(
            entity_id=["sensor.first", "sensor.second"],
            expose_to={"conversation": True},
        )
    captured = [
        manager.read_snapshot(item["name"]) for item in manager.list_snapshots()
    ]
    expected = (
        list(before.values())
        if action.startswith("bulk_")
        else [before["sensor.first"]]
    )
    assert {item["entity_id"] for item in captured} == {row["id"] for row in expected}
    assert all(item["domain"] == "entity_registry" for item in captured)
    assert sorted(
        (item["config"] for item in captured), key=lambda row: row["id"]
    ) == sorted(expected, key=lambda row: row["id"])
    client._request.assert_not_awaited()


async def test_registry_history_follows_renamed_identity_and_clears_new_option_domains(
    registry_history: tuple,
) -> None:
    tools, manager, _, rows, writes = registry_history
    before = copy.deepcopy(rows["sensor.first"])
    await tools.ha_set_entity(entity_id="sensor.first", new_entity_id="sensor.renamed")
    saved = next(
        item for item in manager.list_snapshots() if item["domain"] == "entity_registry"
    )
    rows["sensor.renamed"]["options"]["new_domain"] = {"new": True}
    result = await manager.restore_snapshot(saved["name"])
    assert result["domain"] == "entity_registry" and rows["sensor.first"] == before
    assert "sensor.renamed" not in rows
    assert any(
        payload.get("options_domain") == "new_domain" and payload["options"] is None
        for _, _, payload in writes
    )


async def test_old_entity_state_history_keeps_its_original_domain_semantics(
    registry_history: tuple,
) -> None:
    _, manager, client, _, _ = registry_history
    original = {"state": "old", "attributes": {"unit": "W"}}
    handler = manager.handler_for("entity")
    await handler.restore(client, "sensor.first", original)
    client._request.assert_awaited_once_with(
        "POST", "states/sensor.first", json=original
    )


async def test_registry_restore_refuses_an_edit_made_while_its_safety_copy_is_saved(
    registry_history: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, manager, _, rows, writes = registry_history
    await tools.ha_set_entity(entity_id="sensor.first", name="After")
    saved = manager.list_snapshots()[0]
    capture = manager.maybe_snapshot_known

    async def save_then_edit(*args, **kwargs):
        path = await capture(*args, **kwargs)
        rows["sensor.first"]["name"] = "Concurrent edit"
        return path

    monkeypatch.setattr(manager, "maybe_snapshot_known", save_then_edit)
    previous_writes = copy.deepcopy(writes)
    with pytest.raises(bm.BackupRestoreError) as raised:
        await manager.restore_snapshot(saved["name"])
    assert raised.value.outcome["reason"] == "config_conflict"
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert (
        rows["sensor.first"]["name"] == "Concurrent edit" and writes == previous_writes
    )


async def test_registry_restore_does_not_update_a_replacement_after_first_phase(
    registry_history: tuple,
) -> None:
    tools, manager, client, rows, _ = registry_history
    await tools.ha_set_entity(entity_id="sensor.first", name="After")
    saved = manager.list_snapshots()[0]
    rows["sensor.first"]["options"]["added"] = {"old": True}
    command = client.send_websocket_message.side_effect

    async def replace_after_update(payload: dict) -> dict:
        result = await command(payload)
        if payload["type"] == "config/entity_registry/update" and "name" in payload:
            rows["sensor.first"] = {
                **rows["sensor.first"],
                "id": "replacement",
                "unique_id": "replacement",
                "created_at": 2.0,
                "options": {"added": {"replacement": True}},
            }
        return result

    client.send_websocket_message.side_effect = replace_after_update
    with pytest.raises(bm.BackupRestoreError) as raised:
        await manager.restore_snapshot(saved["name"])
    assert raised.value.outcome["apply_status"] == "unknown"
    assert rows["sensor.first"]["options"] == {"added": {"replacement": True}}


async def test_entity_tool_device_rename_also_captures_the_device_it_modifies(
    registry_history: tuple,
) -> None:
    tools, manager, client, rows, _ = registry_history
    rows["sensor.first"]["device_id"] = "main"
    device = {
        "id": "main",
        "name": "Integration",
        "name_by_user": "Before",
        "area_id": None,
        "disabled_by": None,
        "labels": [],
    }
    before = copy.deepcopy(device)
    manager.register(
        bm.DomainHandler(
            "device",
            AsyncMock(side_effect=lambda *_: copy.deepcopy(device)),
            AsyncMock(),
        )
    )
    command = client.send_websocket_message.side_effect

    async def update_device(payload: dict) -> dict:
        if payload["type"] == "config/device_registry/update":
            device.update(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"type", "device_id"}
                }
            )
            return {"success": True, "result": copy.deepcopy(device)}
        return await command(payload)

    client.send_websocket_message.side_effect = update_device
    await tools.ha_set_entity(
        entity_id="sensor.first",
        name="Changed entity",
        new_device_name="Changed device",
    )
    captured = [
        manager.read_snapshot(item["name"]) for item in manager.list_snapshots()
    ]
    snapshot = next((item for item in captured if item["domain"] == "device"), None)
    assert snapshot is not None and snapshot["config"] == before
    assert snapshot["tool"] == "ha_set_entity"
    assert device["name_by_user"] == "Changed device" and len(captured) == 2


async def test_registry_restore_does_not_confirm_a_last_phase_changed_by_a_listener(
    registry_history: tuple,
) -> None:
    tools, manager, client, rows, _ = registry_history
    await tools.ha_set_entity(entity_id="sensor.first", name="After")
    saved = manager.list_snapshots()[0]
    command = client.send_websocket_message.side_effect

    async def edit_after_ack(payload: dict) -> dict:
        result = await command(payload)
        if payload["type"] == "config/entity_registry/update":
            rows["sensor.first"]["name"] = "Listener changed the final write"
        return result

    client.send_websocket_message.side_effect = edit_after_ack
    with pytest.raises(bm.BackupRestoreError) as raised:
        await manager.restore_snapshot(saved["name"])
    assert raised.value.outcome["apply_status"] == "unknown"
    assert rows["sensor.first"]["name"] == "Listener changed the final write"
