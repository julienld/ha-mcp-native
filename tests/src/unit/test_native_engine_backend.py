"""Native backend runs existing prep/do owners, never their WS handlers."""

from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .test_component_helper_collections import (
    FakeCollection,
    FakeRegistry,
    ItemNotFound,
    _hass,
    hc,
)
from .test_component_ws_phase2_async import _call_hass, _FakeCallServices
from .test_component_ws_search import _REAL_VOL, FakeState, wsapi


class EventBus:
    def __init__(self) -> None:
        self.listeners: list[Any] = []

    def async_listen(self, event_type: Any, callback: Any) -> Any:
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def fire(self, state: Any, context: Any) -> None:
        event = SimpleNamespace(
            context=context, data={"entity_id": state.entity_id, "new_state": state}
        )
        for callback in list(self.listeners):
            callback(event)


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.collection",
        SimpleNamespace(
            StorageCollection=FakeCollection,
            DictStorageCollection=FakeCollection,
            CollectionChange=lambda *values: values,
            CHANGE_ADDED="added",
            CHANGE_UPDATED="updated",
            ItemNotFound=ItemNotFound,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.json",
        SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode()),
    )
    module = importlib.import_module("custom_components.ha_mcp_tools.native_backend")
    monkeypatch.setattr(module, "json_bytes", lambda value: json.dumps(value).encode())
    monkeypatch.setattr(module, "Context", SimpleNamespace)
    monkeypatch.setattr(module, "vol", _REAL_VOL)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    operations = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.services"
    )
    monkeypatch.setattr(operations, "callback", lambda function: function)
    return module


def authorize(hass: Any) -> Any:
    user = SimpleNamespace(
        id="actor", is_admin=True, is_active=True, system_generated=False
    )
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    return user


async def test_native_service_tool_keeps_all_changed_states_and_actor(
    native: Any,
) -> None:
    bus = EventBus()
    services = _FakeCallServices(known={("scene", "turn_on")})
    hass = _call_hass([], services, bus)
    authorize(hass)

    def changed() -> None:
        context = services.calls[0]["context"]
        bus.fire(FakeState("light.a", "on"), context)
        bus.fire(FakeState("switch.b", "on"), context)
        bus.fire(FakeState("sensor.unrelated", "1"), SimpleNamespace(user_id="other"))

    services._on_call = changed
    backend = native.NativeBackend(hass)
    engine = Engine(backend, build_initial_catalog(backend))
    result = await engine.call(
        "ha_call_service",
        {"domain": "scene", "service": "turn_on", "verbose": True},
        CallContext("actor"),
    )
    assert not result.is_error
    assert [state["entity_id"] for state in result.data["result"]] == [
        "light.a",
        "switch.b",
    ]
    assert services.calls[0]["context"].user_id == "actor"
    assert len(services.calls) == 1 and bus.listeners == []
    assert hass.auth.async_get_user.await_count >= 4


async def test_native_confirmation_registers_before_dispatch(native: Any) -> None:
    bus = EventBus()
    services = _FakeCallServices(known={("light", "turn_on")}, response={"future": 42})
    hass = _call_hass([FakeState("light.a", "off")], services, bus)
    authorize(hass)

    def changed() -> None:
        assert len(bus.listeners) == 1
        bus.fire(
            FakeState("light.a", "on", brightness=255), services.calls[0]["context"]
        )

    services._on_call = changed
    backend = native.NativeBackend(hass)
    result = await Engine(backend, build_initial_catalog(backend)).call(
        "ha_call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.a",
            "return_response": True,
            "result_fields": ["entity_id", "state"],
        },
        CallContext("actor"),
    )
    assert result.data["verified_state"] == "on"
    assert result.data["service_response"] == {"future": 42}
    assert result.data["result"] == [{"entity_id": "light.a", "state": "on"}]
    assert len(services.calls) == 1 and bus.listeners == []


async def test_mutate_then_raise_is_not_reissued(native: Any) -> None:
    bus = EventBus()
    services = _FakeCallServices(
        known={("switch", "toggle")}, raises=ValueError("listener failed after write")
    )
    hass = _call_hass([], services, bus)
    authorize(hass)
    backend = native.NativeBackend(hass)
    result = await Engine(backend, build_initial_catalog(backend)).call(
        "ha_call_service",
        {"domain": "switch", "service": "toggle", "wait": False},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert len(services.calls) == 1 and bus.listeners == []


async def test_idempotent_noop_keeps_confirmed_state_result(native: Any) -> None:
    bus = EventBus()
    services = _FakeCallServices(known={("light", "turn_on")})
    hass = _call_hass([FakeState("light.a", "on")], services, bus)
    authorize(hass)
    backend = native.NativeBackend(hass)
    result = await Engine(backend, build_initial_catalog(backend)).call(
        "ha_call_service",
        {"domain": "light", "service": "turn_on", "entity_id": "light.a"},
        CallContext("actor"),
    )
    assert result.data["verified_state"] == "on"
    assert result.data["result"][0]["entity_id"] == "light.a"
    assert services.call_count == 1 and not bus.listeners


@pytest.mark.parametrize("flag", ["is_active", "is_admin", "system_generated"])
async def test_live_actor_rechecked_before_every_operation(
    native: Any, flag: str
) -> None:
    hass = _hass(FakeCollection())
    user = authorize(hass)
    backend = native.NativeBackend(hass)
    await backend.authorize(CallContext("actor"))
    setattr(user, flag, flag == "system_generated")
    with pytest.raises(PermissionError):
        await backend.execute("states.read", {"entity_ids": []}, CallContext("actor"))


async def test_native_helper_calls_collection_owner_and_reads_back(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = FakeCollection()
    registry = FakeRegistry()
    registry.add("input_number.test", "input_number", "test")
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    hass = _hass(collection)
    authorize(hass)
    backend = native.NativeBackend(hass)
    args = {
        "helper_type": "input_number",
        "action": "create",
        "data": {"name": "Test", "future_field": "kept"},
    }
    result = await backend.execute("helpers.storage.write", args, CallContext("actor"))
    readback = await backend.execute(
        "helpers.storage.read",
        {"helper_type": "input_number", "entity_id": "input_number.test"},
        CallContext("actor"),
    )
    assert (
        result["item"]
        == readback["item"]
        == {"id": "test", "name": "Test", "future_field": "kept"}
    )
    assert hc.collection_owner(hass, "input_number").storage_collection is collection


async def test_readonly_and_reserved_envelopes_cannot_reach_owner(native: Any) -> None:
    collection = FakeCollection()
    hass = _hass(collection)
    authorize(hass)
    backend = native.NativeBackend(hass)
    with pytest.raises(ToolFailure, match="Writes are disabled"):
        await backend.execute(
            "helpers.storage.write", {}, CallContext("actor", read_only=True)
        )
    with pytest.raises(ToolFailure, match="Protected"):
        await backend.execute(
            "states.read", {"type": "call_service"}, CallContext("actor")
        )
    with pytest.raises(UnsupportedOperation):
        await backend.execute("arbitrary/ws_command", {}, CallContext("actor"))
    assert collection.data == {}


async def test_arbitrary_ws_is_explicit_native_incompatibility(native: Any) -> None:
    hass = _hass(FakeCollection())
    authorize(hass)
    backend = native.NativeBackend(hass)
    result = await Engine(backend, build_initial_catalog(backend)).call(
        "ha_call_service", {"ws_command": "repairs/list_issues"}, CallContext("actor")
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "UNSUPPORTED_OPERATION"
