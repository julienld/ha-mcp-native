"""Native todo ports retain Core resolution, actor admission and dispatch knowledge."""

import copy
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_todo_items import (
    LIST,
    _Item,
    _items_factory,
    _ListEntity,
)
from .test_component_todo_items import (
    entity as entity,
)
from .test_component_todo_items import (
    hass as hass,
)


@pytest.fixture
def ports(hass, monkeypatch):
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.todo"
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.todo",
        SimpleNamespace(
            DATA_COMPONENT="todo_component",
            TodoListEntity=_ListEntity,
            api_items_factory=_items_factory,
        ),
    )
    monkeypatch.setattr(module, "Context", SimpleNamespace, raising=False)
    hass.data["websocket_api"] = {
        "todo/item/move": (object(), copy.deepcopy),
        "call_service": (object(), copy.deepcopy),
    }
    backend = SimpleNamespace(hass=hass, authorize=AsyncMock())
    return module.NativeTodoOperations(backend), backend


async def test_bridge_resolves_and_updates_core_row_under_actual_actor(ports, hass):
    owner, _backend = ports
    actor = CallContext("human")
    resolved = await owner.execute(
        "todo.bridge", {"entity_id": LIST, "action": "resolve", "refs": ["Milk"]}, actor
    )
    expected = resolved["items"][0]
    result = await owner.execute(
        "todo.bridge",
        {
            "entity_id": LIST,
            "action": "update",
            "uid": "1",
            "data": {"status": "completed"},
            "expected_item": expected,
        },
        actor,
    )
    assert result["item"]["uid"] == "1"
    assert result["item"]["status"] == "completed"
    assert len(hass.services.calls) == 1
    assert hass.services.calls[0][2].user_id == "human"


@pytest.mark.parametrize("action", ["update", "remove"])
async def test_bridge_cas_runs_after_final_admission_await(ports, hass, entity, action):
    owner, backend = ports
    expected = _items_factory(list(vars(entity.todo_items[0]).items()))

    async def admit(actor):
        if backend.authorize.await_count > 1:
            entity.todo_items[0].summary = "Changed while admission awaited"

    backend.authorize.side_effect = admit
    fields = (
        {"uid": "1", "data": {"status": "completed"}, "expected_item": expected}
        if action == "update"
        else {"refs": ["Milk"], "expected_items": [expected]}
    )
    with pytest.raises(ToolFailure) as caught:
        await owner.execute(
            "todo.bridge",
            {"entity_id": LIST, "action": action, **fields},
            CallContext("human"),
        )
    assert caught.value.code == "CONFIG_CONFLICT"
    assert caught.value.status == "not_started"
    assert hass.services.calls == []


async def test_remove_keeps_captured_uid_when_summary_is_reused(ports, hass, entity):
    owner, _backend = ports
    expected = _items_factory(list(vars(entity.todo_items[0]).items()))
    entity.todo_items.insert(0, _Item("new", "Milk"))
    await owner.execute(
        "todo.bridge",
        {
            "entity_id": LIST,
            "action": "remove",
            "refs": ["Milk"],
            "expected_items": [expected],
        },
        CallContext("human"),
    )
    assert hass.services.calls[0][1]["item"] == ["1"]
    assert {item.uid for item in entity.todo_items} == {"new", "2", "3"}


async def test_bridge_provider_error_after_mutation_is_unknown_once(ports, hass):
    owner, _backend = ports
    hass.services.change_then_raise = True
    hass.services.raise_ = ValueError("provider failed after mutation")
    with pytest.raises(ToolFailure) as caught:
        await owner.execute(
            "todo.bridge",
            {"entity_id": LIST, "action": "add", "data": {"item": "Coffee"}},
            CallContext("human"),
        )
    assert caught.value.status == "outcome_unknown"
    assert len(hass.services.calls) == 1


async def test_move_revalidates_admission_and_calls_core_owner(ports, entity):
    owner, backend = ports
    await owner.execute(
        "todo.move",
        {"entity_id": LIST, "uid": "3", "previous_uid": "1"},
        CallContext("human"),
    )
    assert backend.authorize.await_count >= 2
    assert entity.moves == [("3", "1", ["Milk", "Pay", "Milk"])]


async def test_service_preserves_native_response_and_actor(ports, hass):
    owner, _backend = ports
    hass.services.async_call = AsyncMock(
        return_value={LIST: {"items": [{"future": True}]}}
    )
    result = await owner.execute(
        "todo.service",
        {
            "service": "get_items",
            "target": {"entity_id": LIST},
            "service_data": {"status": ["completed"]},
            "return_response": True,
        },
        CallContext("human"),
    )
    assert result == {
        "success": True,
        "result": {"response": {LIST: {"items": [{"future": True}]}}},
    }
    assert hass.services.async_call.await_args.kwargs["context"].user_id == "human"


async def test_read_only_caller_cannot_remove_completed(ports, hass):
    owner, _backend = ports
    with pytest.raises(ToolFailure) as caught:
        await owner.execute(
            "todo.service",
            {
                "service": "remove_completed_items",
                "target": {"entity_id": LIST},
                "service_data": {},
            },
            CallContext("human", read_only=True),
        )
    assert caught.value.status == "not_started"
    assert caught.value.code == "READ_ONLY"
    assert hass.services.calls == []


@pytest.mark.parametrize("removed", [False, True])
async def test_snapshot_restore_uses_native_bridge_and_verifies_stored_row(
    ports, hass, removed
):
    from ha_mcp.runtime.snapshot_todo import restore_todo

    owner, backend = ports
    actor = CallContext("human")

    async def execute(operation, arguments, context):
        if operation == "states.read":
            return {"states": {LIST: {"state": "1"}}}
        return await owner.execute(operation, arguments, context)

    backend.execute = execute
    item = (await execute("todo.items", {"entity_id": LIST}, actor))["result"]["items"][
        0
    ]
    snapshot = {
        "todo_entity_id": LIST,
        "uid": "gone" if removed else "1",
        "summary": "Coffee" if removed else "Milk",
        "status": "completed",
    }
    result = await restore_todo(
        backend,
        actor,
        LIST + "::" + snapshot["uid"],
        snapshot,
        None if removed else {"todo_entity_id": LIST, **item},
    )
    assert result["verification_status"] == "matched"
    assert result["restore_mode"] == ("recreated" if removed else "updated")
    assert result["item"]["status"] == "completed"
    assert [call[0] for call in hass.services.calls] == (
        ["add_item", "update_item"] if removed else ["update_item"]
    )
    assert all(call[2].user_id == "human" for call in hass.services.calls)
