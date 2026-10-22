"""Todo tools share orchestration and dispatch only through the selected owner."""

import copy
import dataclasses
import datetime as dt
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_ws_phase2_async import _call_hass, _FakeCallServices
from .test_component_ws_search import FakeState
from .test_native_engine_backend import EventBus, authorize
from .test_native_engine_backend import native as native


@dataclasses.dataclass
class _Item:
    summary: str
    uid: str
    status: str = "needs_action"
    due: dt.date | None = None
    description: str | None = None


class _TodoEntity:
    def __init__(self) -> None:
        self.todo_items = [
            _Item("Milk", "first", due=dt.date(2026, 10, 23)),
            _Item("Bread", "second", status="completed"),
        ]


def _api_items_factory(values: list[tuple]) -> dict:
    return {
        key: value.isoformat() if isinstance(value, dt.date) else str(value)
        for key, value in values
        if value is not None
    }


def _setup(native: Any, monkeypatch: pytest.MonkeyPatch, backup: Any = None) -> tuple:
    entity = _TodoEntity()
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.todo",
        SimpleNamespace(
            DATA_COMPONENT="todo.component",
            TodoListEntity=_TodoEntity,
            api_items_factory=_api_items_factory,
        ),
    )
    bus = EventBus()
    services = _FakeCallServices(
        known={("todo", action + "_item") for action in ("add", "update", "remove")}
    )
    hass = _call_hass(
        [
            FakeState("todo.shopping", "1", friendly_name="Shopping", icon="mdi:cart"),
            FakeState("sensor.unrelated", "0"),
        ],
        services,
        bus,
    )
    hass.data["todo.component"] = SimpleNamespace(
        get_entity=lambda entity_id: entity if entity_id == "todo.shopping" else None
    )
    user = authorize(hass)
    backend = native.NativeBackend(
        hass, todo_backup=backup or AsyncMock(return_value={})
    )
    engine = Engine(
        backend,
        [
            definition
            for definition in build_initial_catalog(backend)
            if definition.name
            in {"ha_get_todo", "ha_set_todo_item", "ha_remove_todo_item"}
        ],
    )
    return engine, hass, services, user


async def test_native_todo_reads_core_entity_items_and_filter_without_transport(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, services, _ = _setup(native, monkeypatch)
    actor = CallContext("actor")
    lists = await engine.call("ha_get_todo", {}, actor)
    assert not lists.is_error and lists.data["todo_lists"] == [
        {
            "entity_id": "todo.shopping",
            "friendly_name": "Shopping",
            "state": "1",
            "icon": "mdi:cart",
            "supported_features": None,
        }
    ]
    items = await engine.call(
        "ha_get_todo", {"entity_id": "todo.shopping", "status": "needs_action"}, actor
    )
    assert not items.is_error and items.data["items"] == [
        {
            "summary": "Milk",
            "uid": "first",
            "status": "needs_action",
            "due": "2026-10-23",
        }
    ]
    assert services.calls == []


async def test_native_todo_add_uses_actual_actor_and_changed_state_body(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock()
    engine, hass, services, _ = _setup(native, monkeypatch, backup)

    def changed() -> None:
        hass.bus.fire(FakeState("todo.shopping", "2"), services.calls[0]["context"])
        hass.bus.fire(FakeState("todo.foreign", "9"), SimpleNamespace(user_id="other"))

    services._on_call = changed
    result = await engine.call(
        "ha_set_todo_item",
        {
            "entity_id": "todo.shopping",
            "summary": "Coffee",
            "description": "Beans",
            "due_date": "2026-10-23",
            "due_datetime": "2026-10-24T12:00:00",
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert len(services.calls) == 1
    call = services.calls[0]
    assert call["domain"] == "todo" and call["service"] == "add_item"
    assert call["service_data"] == {
        "entity_id": "todo.shopping",
        "item": "Coffee",
        "description": "Beans",
        "due_datetime": "2026-10-24T12:00:00",
    }
    assert call["context"].user_id == "actor"
    assert [state["entity_id"] for state in result.data["result"]] == ["todo.shopping"]
    assert hass.bus.listeners == []
    backup.assert_not_awaited()


@pytest.mark.parametrize("remove", [False, True])
async def test_native_todo_updates_and_deletes_capture_once_before_dispatch(
    native: Any, monkeypatch: pytest.MonkeyPatch, remove: bool
) -> None:
    captures = []

    async def capture(payload: dict, actor: CallContext) -> dict:
        assert services.calls == [] and actor.user_id == "actor"
        captures.append(copy.deepcopy(payload))
        return {"warnings": ["Snapshot could not be persisted"]}

    engine, _, services, _ = _setup(native, monkeypatch, capture)
    tool = "ha_remove_todo_item" if remove else "ha_set_todo_item"
    result = await engine.call(
        tool,
        {
            "entity_id": "todo.shopping",
            "item": "first",
            **({} if remove else {"status": "completed"}),
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert captures == [
        {
            "entity_id": "todo.shopping",
            "item": "first",
            "source_tool": tool,
            "current_item": {
                "summary": "Milk",
                "uid": "first",
                "status": "needs_action",
                "due": "2026-10-23",
            },
        }
    ]
    assert result.data["warnings"] == ["Snapshot could not be persisted"]
    assert len(services.calls) == 1
    assert services.calls[0]["service"] == ("remove_item" if remove else "update_item")


@pytest.mark.parametrize(
    "arguments",
    [
        {"entity_id": "todo.shopping", "item": " ", "status": "completed"},
        {"entity_id": "todo.shopping", "item": "first"},
        {"entity_id": "light.wrong", "summary": "Invalid"},
        {"entity_id": "todo.shopping", "summary": "Invalid", "rename": "No"},
    ],
)
async def test_native_todo_invalid_mutations_never_capture_or_dispatch(
    native: Any, monkeypatch: pytest.MonkeyPatch, arguments: dict
) -> None:
    backup = AsyncMock()
    engine, _, services, _ = _setup(native, monkeypatch, backup)
    result = await engine.call("ha_set_todo_item", arguments, CallContext("actor"))
    assert result.is_error
    assert services.calls == []
    backup.assert_not_awaited()


async def test_native_todo_readonly_still_lists_but_never_starts_writes(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, services, _ = _setup(native, monkeypatch)
    engine.read_only = True
    assert not (await engine.call("ha_get_todo", {}, CallContext("actor"))).is_error
    with pytest.raises(ToolFailure, match="Writes are disabled"):
        await engine.call(
            "ha_set_todo_item",
            {"entity_id": "todo.shopping", "summary": "Milk"},
            CallContext("actor"),
        )
    assert services.calls == []


@pytest.mark.parametrize("revoke", [False, True])
async def test_native_todo_rechecks_live_policy_and_credentials_after_snapshot(
    native: Any, monkeypatch: pytest.MonkeyPatch, revoke: bool
) -> None:
    async def capture(payload: dict, actor: CallContext) -> dict:
        if revoke:
            user.is_admin = False
        else:
            engine.read_only = True
        return {}

    engine, _, services, user = _setup(native, monkeypatch, capture)
    result = await engine.call(
        "ha_set_todo_item",
        {"entity_id": "todo.shopping", "item": "first", "status": "completed"},
        CallContext("actor"),
    )
    assert result.is_error and services.calls == []


async def test_native_todo_provider_mutation_then_failure_is_uncertain_and_not_replayed(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, hass, services, _ = _setup(native, monkeypatch)
    services._raises = ValueError("Provider changed its data before raising")
    result = await engine.call(
        "ha_set_todo_item",
        {"entity_id": "todo.shopping", "summary": "Milk"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    assert len(services.calls) == 1 and hass.bus.listeners == []


async def test_native_todo_change_during_snapshot_is_refused_without_dispatch(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture(payload: dict, actor: CallContext) -> dict:
        entity.todo_items[0].summary = "Changed externally"
        return {}

    engine, hass, services, _ = _setup(native, monkeypatch, capture)
    entity = hass.data["todo.component"].get_entity("todo.shopping")
    result = await engine.call(
        "ha_remove_todo_item",
        {"entity_id": "todo.shopping", "item": "Milk"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert services.calls == []


async def test_native_todo_uid_summary_collision_is_refused_before_snapshot(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = AsyncMock()
    engine, hass, services, _ = _setup(native, monkeypatch, backup)
    entity = hass.data["todo.component"].get_entity("todo.shopping")
    entity.todo_items.insert(0, _Item("first", "collision"))
    result = await engine.call(
        "ha_remove_todo_item",
        {"entity_id": "todo.shopping", "item": "Milk"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert services.calls == []
    backup.assert_not_awaited()


async def test_native_todo_freezes_original_match_when_summary_is_reused_during_snapshot(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture(payload: dict, actor: CallContext) -> dict:
        entity.todo_items.insert(0, _Item("Milk", "new"))
        return {}

    engine, hass, services, _ = _setup(native, monkeypatch, capture)
    entity = hass.data["todo.component"].get_entity("todo.shopping")
    result = await engine.call(
        "ha_remove_todo_item",
        {"entity_id": "todo.shopping", "item": "Milk"},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert services.calls[0]["service_data"]["item"] == "first"


async def test_legacy_todo_facade_preserves_remote_routes_and_common_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools.tools_todo import TodoTools as LegacyTodoTools

    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False),
    )
    client = SimpleNamespace(
        get_states=AsyncMock(return_value=[]),
        send_websocket_message=AsyncMock(
            return_value={
                "success": True,
                "result": {"items": [{"uid": "u", "status": "completed"}]},
            }
        ),
        call_service=AsyncMock(return_value=[]),
    )
    tools = LegacyTodoTools(client)
    read = await tools.ha_get_todo(entity_id="todo.shopping", status="completed")
    assert read["items"] == [{"uid": "u", "status": "completed"}]
    client.send_websocket_message.assert_awaited_once_with(
        {"type": "todo/item/list", "entity_id": "todo.shopping"}
    )
    updated = await tools.ha_set_todo_item(
        entity_id="todo.shopping", item="u", status="completed"
    )
    assert updated["success"]
    client.call_service.assert_awaited_once_with(
        "todo",
        "update_item",
        {"entity_id": "todo.shopping", "item": "u", "status": "completed"},
    )
