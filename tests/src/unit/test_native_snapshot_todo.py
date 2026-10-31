"""Todo recovery translates the stored format into Core's service contract."""

import copy

import pytest

from .test_native_snapshot_restore import runtime as runtime


@pytest.fixture
def todos(runtime):
    manager, snapshots, store, *_rest = runtime
    items, writes = [], []

    async def execute(op, args, actor):
        if op == "todo.items":
            return {"success": True, "result": {"items": copy.deepcopy(items)}}
        data = args["data"]
        assert set(data) <= {
            "entity_id",
            "item",
            "rename",
            "status",
            "description",
            "due_date",
            "due_datetime",
        }
        writes.append((op, copy.deepcopy(args)))
        if op == "todo.item.add":
            items.append(
                {"uid": "new-uid", "summary": data["item"], "status": "needs_action"}
            )
            item = items[-1]
        else:
            item = next(item for item in items if item["uid"] == data["item"])
            assert item == args["expected_item"]
            item.update(summary=data["rename"], status=data["status"])
        for key in ("description", "due_date", "due_datetime"):
            if key in data:
                target = "due" if key.startswith("due_") else key
                if data[key] is None:
                    item.pop(target, None)
                else:
                    item[target] = data[key]
        return []

    manager.backend.execute = execute
    return manager, snapshots, store, items, writes


async def test_removed_completed_todo_is_recreated_with_date_and_status_then_reused(
    todos,
):
    from ha_mcp.tool_api import CallContext

    _manager, snapshots, store, items, writes = todos
    saved = {
        "todo_entity_id": "todo.list",
        "uid": "old-uid",
        "summary": "Milk",
        "status": "completed",
        "due": "2026-11-01",
        "description": "Lactose free",
    }
    path = store.capture("todo_item", "todo.list::Milk", saved, "fixture")
    first = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert first["result"]["restore_mode"] == "recreated"
    assert items == [
        {**{k: v for k, v in saved.items() if k != "todo_entity_id"}, "uid": "new-uid"}
    ]
    assert [op for op, _ in writes] == ["todo.item.add", "todo.item.update"]
    assert writes[0][1]["data"]["due_date"] == saved["due"]
    second = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert second["result"]["restore_mode"] == "already_recreated"
    assert len(writes) == 2


async def test_existing_todo_restore_updates_saved_uid_and_clears_new_optional_values(
    todos,
):
    from ha_mcp.tool_api import CallContext

    _manager, snapshots, store, items, writes = todos
    items.extend(
        [
            {"uid": "other", "summary": "Milk", "status": "needs_action"},
            {
                "uid": "saved",
                "summary": "Renamed",
                "status": "completed",
                "description": "new",
                "due": "2026-11-01T12:00:00-04:00",
            },
        ]
    )
    path = store.capture(
        "todo_item",
        "todo.list::Milk",
        {
            "todo_entity_id": "todo.list",
            "uid": "saved",
            "summary": "Milk",
            "status": "needs_action",
        },
        "fixture",
    )
    result = await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert result["result"]["restore_mode"] == "updated"
    assert items[0]["uid"] == "other"
    assert items[1] == {"uid": "saved", "summary": "Milk", "status": "needs_action"}
    assert writes[0][1]["data"]["item"] == "saved"
    assert writes[0][1]["data"]["due_datetime"] is None
    assert (
        store.read_snapshot(result["safety_backup"])["config"]["summary"] == "Renamed"
    )


async def test_todo_create_remains_applied_when_status_completion_is_revoked(todos):
    from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
    from ha_mcp.tool_api import CallContext, ToolFailure

    manager, snapshots, store, items, writes = todos
    invoke = manager.backend.execute

    async def execute(op, args, actor):
        if op == "todo.item.update":
            raise ToolFailure("READ_ONLY", "Writes revoked", status="not_started")
        return await invoke(op, args, actor)

    manager.backend.execute = execute
    path = store.capture(
        "todo_item",
        "todo.list::Milk",
        {
            "todo_entity_id": "todo.list",
            "uid": "old",
            "summary": "Milk",
            "status": "completed",
        },
        "fixture",
    )
    with pytest.raises(BackupRestoreError) as caught:
        await snapshots.restore_snapshot(path.name, CallContext("owner"))
    assert caught.value.outcome["apply_status"] == "applied"
    assert items[0]["status"] == "needs_action"
    assert len(writes) == 1
