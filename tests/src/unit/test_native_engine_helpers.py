"""Helper semantic ports preserve the remote client's at-most-once boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.helper_tools import HelperConfigTools
from ha_mcp.tool_api import CallContext

from .test_component_helper_collections import FakeCollection, FakeRegistry, _hass
from .test_component_ws_search import FakeRegEntry, make_view, wsapi
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native
from .test_native_engine_flows import flows as flows


def helper_engine(backend: object) -> Engine:
    definition = define_tool(
        backend,
        HelperConfigTools,
        "ha_config_set_helper",
        annotations={"readOnlyHint": False},
    )
    return Engine(backend, [definition])


async def test_remote_helper_storage_roundtrip_uses_existing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools import component_helper_collections

    monkeypatch.setattr(
        component_helper_collections, "_component_ws", AsyncMock(return_value=None)
    )
    stored = {}

    async def send(message: dict) -> dict:
        match message["type"]:
            case "input_boolean/create":
                stored.update(
                    {
                        "id": "example",
                        **{
                            key: value
                            for key, value in message.items()
                            if key != "type"
                        },
                    }
                )
                return {"success": True, "result": dict(stored)}
            case "input_boolean/list":
                return {"success": True, "result": [dict(stored)]}
            case "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "input_boolean.renamed",
                            "platform": "input_boolean",
                            "unique_id": "example",
                        }
                    ],
                }
        raise AssertionError(message)

    client = SimpleNamespace(send_websocket_message=AsyncMock(side_effect=send))
    backend = RemoteBackend(client)
    context = CallContext("user")
    result = await backend.execute(
        "helpers.storage.write",
        {
            "helper_type": "input_boolean",
            "action": "create",
            "data": {"name": "Example", "initial": False},
        },
        context,
    )
    readback = await backend.execute(
        "helpers.storage.read",
        {"helper_type": "input_boolean", "item_id": "example"},
        context,
    )
    assert (
        result["item"]
        == readback["item"]
        == {"id": "example", "name": "Example", "initial": False}
    )
    assert result["entity_id"] == readback["entity_id"] == "input_boolean.renamed"
    assert (
        sum(
            call.args[0]["type"] == "input_boolean/create"
            for call in client.send_websocket_message.await_args_list
        )
        == 1
    )


async def test_legacy_public_helper_uses_shared_storage_and_captures_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.config import get_global_settings
    from ha_mcp.tools import component_helper_collections
    from ha_mcp.tools.tools_config_helpers import HelperConfigTools as LegacyHelperTools

    monkeypatch.setattr(
        component_helper_collections, "_component_ws", AsyncMock(return_value=None)
    )
    settings = get_global_settings().model_copy(update={"enable_auto_backup": True})
    monkeypatch.setattr("ha_mcp.config.get_global_settings", lambda: settings)
    capture = AsyncMock()
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_backup_manager",
        lambda *_args, **_kwargs: SimpleNamespace(maybe_snapshot=capture),
    )
    current = {"id": "one", "name": "Before", "initial": False}
    entry = {
        "entity_id": "input_boolean.renamed",
        "platform": "input_boolean",
        "unique_id": "one",
    }

    async def send(message: dict) -> dict:
        if message["type"] == "input_boolean/list":
            value = [dict(current)]
        elif message["type"] == "config/entity_registry/list":
            value = [entry]
        elif message["type"] == "input_boolean/update":
            current.update(
                {
                    key: value
                    for key, value in message.items()
                    if key not in {"type", "input_boolean_id"}
                }
            )
            value = dict(current)
        else:
            raise AssertionError(message)
        return {"success": True, "result": value}

    client = Mock(send_websocket_message=AsyncMock(side_effect=send))
    result = await LegacyHelperTools(client).ha_config_set_helper(
        helper_type="input_boolean",
        helper_id="one",
        name="After",
        wait=False,
        MandatoryBPS=False,
    )
    assert result["success"] and result["data"]["name"] == "After"
    assert result["entity_id"] == "input_boolean.renamed"
    assert current["initial"] is False
    assert capture.await_count == 1
    assert capture.await_args.args[:2] == ("helper_input_boolean", "one")
    assert (
        sum(
            call.args[0]["type"] == "input_boolean/update"
            for call in client.send_websocket_message.await_args_list
        )
        == 1
    )


async def test_helper_write_unknown_outcome_does_not_fallback_or_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.client.rest_client import HomeAssistantCommandTimeout
    from ha_mcp.tools import component_helper_collections

    socket = SimpleNamespace(
        send_command=AsyncMock(side_effect=HomeAssistantCommandTimeout("reply lost"))
    )
    monkeypatch.setattr(
        component_helper_collections, "_component_ws", AsyncMock(return_value=socket)
    )
    client = SimpleNamespace(send_websocket_message=AsyncMock())
    backend = RemoteBackend(client)
    context = CallContext("user")
    result = await Engine(backend).call_owned(
        "helper-write",
        lambda: backend.execute(
            "helpers.storage.write",
            {
                "helper_type": "input_boolean",
                "action": "create",
                "data": {"name": "Example"},
            },
            context,
        ),
        context,
        writes=True,
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["write_outcome_unknown"] is True
    socket.send_command.assert_awaited_once()
    client.send_websocket_message.assert_not_awaited()


async def test_shared_public_helper_creates_updates_and_snapshots_native(
    native: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = FakeCollection()
    registry = FakeRegistry()
    registry.add("input_boolean.renamed", "input_boolean", "example")
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    hass = _hass(collection, "input_boolean")
    authorize(hass)
    captured = []

    async def backup(payload: dict, context: CallContext) -> dict:
        assert collection.data["example"]["initial"] is False
        captured.append(payload)
        return {}

    backend = native.NativeBackend(hass, helper_backup=backup)
    engine = helper_engine(backend)
    created = await engine.call(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": "Example",
            "config": {"initial": False},
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert not created.is_error, created.data
    assert created.data["entity_id"] == "input_boolean.renamed"
    updated = await engine.call(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "helper_id": "input_boolean.renamed",
            "name": "Renamed",
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert not updated.is_error, updated.data
    assert updated.data["data"]["initial"] is False
    assert collection.data["example"]["name"] == "Renamed"
    assert captured[0]["item_id"] == "example"
    assert captured[0]["backup_id"] == "input_boolean.renamed"


async def test_snapshot_await_cannot_overwrite_a_newer_native_item(
    native: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = FakeCollection(
        {"example": {"id": "example", "name": "Example", "initial": False}}
    )
    registry = FakeRegistry()
    registry.add("input_boolean.example", "input_boolean", "example")
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    hass = _hass(collection, "input_boolean")
    authorize(hass)

    async def backup(payload: dict, context: CallContext) -> None:
        collection.data["example"]["initial"] = True

    result = await helper_engine(native.NativeBackend(hass, helper_backup=backup)).call(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "helper_id": "example",
            "name": "Should not land",
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert collection.data["example"]["name"] == "Example"


async def test_remote_snapshot_conflict_distinguishes_boolean_and_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tool_api import ToolFailure
    from ha_mcp.tools import component_helper_collections

    monkeypatch.setattr(
        component_helper_collections,
        "read_stored_helper_item",
        AsyncMock(return_value={"success": True, "item": {"id": "one", "initial": 0}}),
    )
    write = AsyncMock()
    monkeypatch.setattr(component_helper_collections, "write_helper_item", write)
    with pytest.raises(ToolFailure, match="changed after its snapshot"):
        await RemoteBackend(SimpleNamespace()).execute(
            "helpers.storage.write",
            {
                "helper_type": "input_boolean",
                "action": "update",
                "item_id": "one",
                "data": {"name": "AI"},
                "expected_item": {"id": "one", "initial": False},
            },
            CallContext("actor"),
        )
    write.assert_not_awaited()


async def test_shared_helper_runs_one_native_utility_meter_flow(
    native: object, flows: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(
            async_get_config_flows=AsyncMock(return_value={"utility_meter"})
        ),
    )
    hass = _hass(FakeCollection())
    authorize(hass)
    flow = SimpleNamespace(
        async_init=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "meter-flow",
                "data_schema": [
                    {"name": "name"},
                    {"name": "source"},
                    {"name": "cycle"},
                ],
            }
        ),
        async_configure=AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "meter-flow",
                "result": {"entry_id": "meter-entry", "title": "Daily Meter"},
            }
        ),
        async_abort=Mock(),
    )
    hass.config_entries = SimpleNamespace(flow=flow, async_entries=lambda domain: [])
    view = make_view(
        entity={
            "sensor.daily_meter": FakeRegEntry(
                "sensor.daily_meter",
                config_entry_id="meter-entry",
                platform="utility_meter",
            )
        }
    )
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    backend = native.NativeBackend(hass)
    result = await helper_engine(backend).call(
        "ha_config_set_helper",
        {
            "helper_type": "utility_meter",
            "name": "Daily Meter",
            "config": {
                "source": "sensor.energy",
                "cycle": "daily",
                "not_a_declared_field": 2,
            },
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["method"] == "config_flow"
    assert result.data["entity_ids"] == ["sensor.daily_meter"]
    flow.async_configure.assert_awaited_once_with(
        "meter-flow",
        {"name": "Daily Meter", "source": "sensor.energy", "cycle": "daily"},
    )
    assert backend._flows.owned == {}


async def test_same_public_flow_uses_existing_remote_flow_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.tools.config_helpers import registry as helper_registry

    monkeypatch.setattr(
        helper_registry,
        "fetch_entities_for_config_entry_via_component",
        AsyncMock(
            return_value=[
                {"entity_id": "sensor.daily_meter", "config_entry_id": "meter-entry"}
            ]
        ),
    )
    client = SimpleNamespace(
        _request=AsyncMock(return_value=["utility_meter"]),
        abort_config_flow=AsyncMock(return_value={}),
        send_websocket_message=AsyncMock(return_value={"success": True, "result": []}),
        start_config_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "flow",
                "data_schema": [
                    {"name": "name"},
                    {"name": "source"},
                    {"name": "cycle"},
                ],
            }
        ),
        submit_config_flow_step=AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow",
                "result": {"entry_id": "meter-entry", "title": "Daily Meter"},
            }
        ),
    )
    backend = RemoteBackend(client)
    result = await helper_engine(backend).call(
        "ha_config_set_helper",
        {
            "helper_type": "utility_meter",
            "name": "Daily Meter",
            "config": {"source": "sensor.energy", "cycle": "daily"},
            "MandatoryBPS": False,
        },
        CallContext("remote"),
    )
    assert not result.is_error, result.data
    assert result.data == {
        "success": True,
        "action": "create",
        "helper_type": "utility_meter",
        "data": {"entry_id": "meter-entry", "title": "Daily Meter"},
        "message": "utility_meter helper created successfully",
        "method": "config_flow",
        "entry_id": "meter-entry",
        "title": "Daily Meter",
        "entity_ids": ["sensor.daily_meter"],
    }
    assert client.start_config_flow.await_args_list == [
        call("utility_meter"),
        call("utility_meter"),
    ]
    client.abort_config_flow.assert_awaited_once_with("flow")
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow", {"name": "Daily Meter", "source": "sensor.energy", "cycle": "daily"}
    )


@pytest.mark.parametrize("change", ["read_only", "disabled", "revoked"])
async def test_helper_rechecks_policy_and_actor_after_snapshot_await(
    native: object, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    collection = FakeCollection(
        {"example": {"id": "example", "name": "Original", "initial": False}}
    )
    registry = FakeRegistry()
    registry.add("input_boolean.example", "input_boolean", "example")
    monkeypatch.setattr(wsapi.er, "async_get", lambda hass: registry)
    hass = _hass(collection, "input_boolean")
    user = authorize(hass)

    async def backup(payload: dict, context: CallContext) -> None:
        if change == "read_only":
            engine.read_only = True
        elif change == "disabled":
            engine.enabled_tools = set()
        else:
            user.is_active = False

    engine = helper_engine(native.NativeBackend(hass, helper_backup=backup))
    result = await engine.call(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "helper_id": "example",
            "name": "Blocked",
            "MandatoryBPS": False,
        },
        CallContext("actor"),
    )
    assert result.is_error and collection.data["example"]["name"] == "Original"
