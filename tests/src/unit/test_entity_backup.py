"""Registry history keeps durable identities inside public entity-ID captures.

Retired scenarios and their canonical coverage are recorded in
docs/native-migration/entity-backup-test-migration.json.
"""

import ast
import copy
import json
from pathlib import Path

import pytest

from ha_mcp.backup_manager import BackupRestoreError, InvalidBackupSnapshotError
from ha_mcp.errors import HomeAssistantError
from ha_mcp.tools.backup_on_demand import capture_on_demand

from .test_entity_registry_snapshots import registry_history as registry_history


@pytest.fixture
def history(registry_history):
    tools, manager, client, rows, writes = registry_history
    rows["sensor.first"]["categories"] = {"automation": "saved-category"}
    rows["sensor.first"]["options"].update(
        conversation={"should_expose": False},
        **{"cloud.alexa": {"should_expose": True}},
    )
    command = client.send_websocket_message.side_effect

    async def core_update(payload):
        if payload["type"] == "config/category_registry/list":
            assert payload["scope"] == "automation"
            return {
                "success": True,
                "result": [{"category_id": "saved-category", "name": "Saved category"}],
            }
        # Core merges category scopes, removing only those explicitly set null.
        if (
            payload["type"] == "config/entity_registry/update"
            and "categories" in payload
        ):
            categories = {
                **rows[payload["entity_id"]]["categories"],
                **payload["categories"],
            }
            payload = {
                **payload,
                "categories": {
                    key: value for key, value in categories.items() if value is not None
                },
            }
        result = await command(payload)
        if (
            payload["type"] == "config/entity_registry/update"
            and "disabled_by" in payload
            and payload["disabled_by"] is None
        ):
            result["result"]["require_restart"] = True
        return result

    client.send_websocket_message.side_effect = core_update
    return tools, manager, client, rows, writes


@pytest.mark.parametrize(
    "edit",
    [
        {"name": "After"},
        {"name": ""},
        {"icon": "mdi:lamp"},
        {"area_id": "kitchen"},
        {"area_id": None},
        {"device_class": "outlet"},
        {"aliases": [None, "lamp"]},
        {"labels": ["changed"]},
        {"categories": {"automation": "changed"}},
        {"categories": {"script": "new-scope"}},
        {"categories": {"automation": None}},
        {"hidden_by": "user"},
        {"disabled_by": "user"},
        {"entity_id": "sensor.renamed"},
        {"entity_id": "sensor.renamed", "name": "Renamed", "hidden_by": "user"},
        {"options": {"sensor": {"display_precision": 2}}},
        {"options": {}},
        {"options": {"conversation": {"should_expose": True}}},
        {
            "entity_id": "sensor.renamed",
            "options": {"cloud.alexa": {"should_expose": False}},
        },
    ],
)
async def test_registry_backup_restores_each_user_setting(history, edit):
    _, manager, client, rows, _ = history
    before = copy.deepcopy(rows["sensor.first"])
    path = await capture_on_demand(manager, "entity_registry", "sensor.first")
    changed = rows.pop("sensor.first")
    changed.update(copy.deepcopy(edit))
    if "categories" in edit:
        changed["categories"] = {
            key: value
            for key, value in {**before["categories"], **edit["categories"]}.items()
            if value is not None
        }
    rows[changed["entity_id"]] = changed

    result = await manager.restore_snapshot(path.name)

    assert rows["sensor.first"] == before
    assert result["domain"] == "entity_registry" and result["entity_id"] == before["id"]
    safety = manager.read_snapshot(result["safety_backup"])
    assert safety["domain"] == "entity"
    assert safety["entity_id"] == edit.get("entity_id", "sensor.first")
    assert safety["config"]["entity_id"] == safety["entity_id"]
    assert safety["config"]["id"] == before["id"]
    client._request.assert_not_awaited()


async def test_missing_registry_entry_creates_no_history(history):
    _, manager, client, _, _ = history
    assert (
        await manager.handler_for("entity_registry").fetch(client, "sensor.absent")
        is None
    )
    assert await manager.maybe_snapshot("entity_registry", "sensor.absent") is None
    assert manager.list_snapshots() == []


@pytest.mark.parametrize("disabled", [False, True])
async def test_only_enabling_registry_restore_propagates_core_restart_flag(
    history, disabled
):
    _, manager, _, rows, _ = history
    path = await capture_on_demand(manager, "entity_registry", "sensor.first")
    rows["sensor.first"]["name"] = "After"
    if disabled:
        rows["sensor.first"]["disabled_by"] = "user"
    outcome = await manager.restore_snapshot(path.name)
    assert bool(outcome["result"].get("require_restart")) is disabled


async def test_registry_capture_propagates_read_failure(history):
    _, manager, client, _, writes = history
    client.send_websocket_message.side_effect = HomeAssistantError(
        "Registry unavailable"
    )
    with pytest.raises(HomeAssistantError, match="Registry unavailable"):
        await manager.handler_for("entity_registry").fetch(client, "uuid-first")
    assert writes == [] and manager.list_snapshots() == []


@pytest.mark.parametrize(
    "reason", ["invalid_reference", "hidden_by", "disabled_device"]
)
async def test_core_refusal_of_registry_restore_never_changes_the_entity(
    history, reason
):
    _, manager, client, rows, writes = history
    path = await capture_on_demand(manager, "entity_registry", "sensor.first")
    rows["sensor.first"]["name"] = "Keep this edit"
    before = copy.deepcopy(rows)
    command = client.send_websocket_message.side_effect

    async def refuse(payload):
        if payload["type"] == "config/entity_registry/update":
            return {"success": False, "error": {"code": reason, "message": reason}}
        return await command(payload)

    client.send_websocket_message.side_effect = refuse
    with pytest.raises(HomeAssistantError, match=reason):
        await manager.restore_snapshot(path.name)
    assert rows == before and writes == []


def test_retired_scenarios_reference_existing_canonical_tests():
    root = Path(__file__).resolve().parents[3]
    migration = json.loads(
        (root / "docs/native-migration/entity-backup-test-migration.json").read_text(
            encoding="utf-8"
        )
    )
    retired = [record["retired"] for record in migration["records"]]
    assert len(retired) == len(set(retired))
    for record in migration["records"]:
        assert record["reason"] and record["status"] and record["coverage"], record
        for nodeid in record["coverage"]:
            path, *names = nodeid.split("::")
            body = ast.parse((root / path).read_text(encoding="utf-8-sig")).body
            for name in names:
                matches = [node for node in body if getattr(node, "name", None) == name]
                assert len(matches) == 1, nodeid
                body = getattr(matches[0], "body", [])


@pytest.mark.parametrize("change", ["rename", "replacement"])
async def test_registry_restore_rejects_identity_changes_during_safety_io(
    history, monkeypatch, change
):
    _, manager, _, rows, writes = history
    path = await capture_on_demand(manager, "entity_registry", "sensor.first")
    rows["sensor.first"]["name"] = "After"
    capture = manager.maybe_snapshot_known

    async def change_after_capture(*args, **kwargs):
        result = await capture(*args, **kwargs)
        row = rows.pop("sensor.first")
        if change == "rename":
            row["entity_id"] = "sensor.third"
        else:
            row.update(id="replacement", unique_id="replacement", created_at=2.0)
        rows[row["entity_id"]] = row
        return result

    monkeypatch.setattr(manager, "maybe_snapshot_known", change_after_capture)
    with pytest.raises(BackupRestoreError) as raised:
        await manager.restore_snapshot(path.name)
    assert raised.value.outcome["apply_status"] == "not_applied"
    assert rows["sensor.second"]["name"] == "Before"
    assert any(row["name"] == "After" for row in rows.values())
    assert writes == []


async def test_deleted_registry_history_does_not_recreate_a_state_or_registration(
    history,
):
    _, manager, _, rows, writes = history
    path = await capture_on_demand(manager, "entity_registry", "sensor.first")
    rows.pop("sensor.first")
    with pytest.raises(BackupRestoreError) as raised:
        await manager.restore_snapshot(path.name)
    assert raised.value.outcome["reason"] == "entry_deleted"
    assert "sensor.first" not in rows and writes == []


@pytest.mark.parametrize(
    "saved", ["on", {"entity_id": "sensor.first", "state": "on", "attributes": {}}]
)
async def test_state_payload_cannot_be_restored_as_registry_history(history, saved):
    _, manager, client, rows, writes = history
    before = copy.deepcopy(rows)
    client.send_websocket_message.reset_mock()
    with pytest.raises(InvalidBackupSnapshotError):
        await manager.handler_for("entity_registry").restore(
            client, "uuid-first", saved
        )
    client.send_websocket_message.assert_not_awaited()
    assert rows == before and writes == []
