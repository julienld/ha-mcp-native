"""Category writes delegate validation, preserve snapshots and compare Core rows."""

import copy
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.category_writes import CategoryWriteTools
from ha_mcp.tool_api import CallContext

from .test_component_ws_search import FakeHass
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


class _CategoryRegistry:
    def __init__(self) -> None:
        self.rows = {
            ("automation", "cat"): {
                "category_id": "cat",
                "name": "Before",
                "icon": None,
                "created_at": 1.0,
                "modified_at": 2.0,
                "future_core_field": False,
            }
        }
        self.calls = []

    def async_get_category(self, *, scope: str, category_id: str) -> Any:
        return self.rows.get((scope, category_id))

    def async_create(self, *, scope: str, name: str, **data: Any) -> dict:
        row = {
            "category_id": "new",
            "name": name,
            "future_core_field": "preserved",
            **data,
        }
        self.rows[(scope, "new")] = row
        self.calls.append(("create", scope, "new"))
        return row

    def async_update(self, *, scope: str, category_id: str, **data: Any) -> dict:
        row = self.rows[(scope, category_id)]
        row.update(data)
        self.calls.append(("update", scope, category_id))
        return row

    def async_delete(self, *, scope: str, category_id: str) -> None:
        del self.rows[(scope, category_id)]
        self.calls.append(("delete", scope, category_id))


def _category_setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    hass = FakeHass()
    authorize(hass)
    registry = _CategoryRegistry()
    owner = SimpleNamespace(async_get=lambda hass: registry)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "category_registry", owner, raising=False
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.category_registry", owner)
    serializer = copy.deepcopy
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.config.category_registry",
        SimpleNamespace(_entry_dict=serializer),
    )
    validated = []

    def schema(payload: dict) -> dict:
        validated.append(copy.deepcopy(payload))
        return copy.deepcopy(payload)

    hass.data["websocket_api"] = {
        f"config/category_registry/{action}": (None, schema)
        for action in ("create", "update", "delete")
    }
    backend = native.NativeBackend(
        hass, registry_backup=backup or AsyncMock(return_value={})
    )
    definitions = [
        define_tool(
            backend, CategoryWriteTools, name, annotations={"readOnlyHint": False}
        )
        for name in ("ha_config_set_category", "ha_config_remove_category")
    ]
    return Engine(backend, definitions), hass, registry, validated


async def test_native_category_create_update_delete_keep_scoped_snapshots_and_core_fields(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, actor: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {"warnings": ["Snapshot warning"]} if payload["backup_id"] else {}

    engine, _, registry, validated = _category_setup(native, monkeypatch, backup=backup)
    before = copy.deepcopy(registry.rows[("automation", "cat")])
    updated = await engine.call(
        "ha_config_set_category",
        {
            "scope": "automation",
            "category_id": "cat",
            "name": "Updated",
            "icon": "mdi:tag",
        },
        CallContext("actor"),
    )
    assert not updated.is_error, updated.data
    assert updated.data["category_data"]["future_core_field"] is False
    assert updated.data["warnings"] == ["Snapshot warning"]
    assert captured[0]["backup_id"] == "automation:cat" and captured[0][
        "current_config"
    ] == {"scope": "automation", **before}
    created = await engine.call(
        "ha_config_set_category",
        {"scope": "script", "name": "New"},
        CallContext("actor"),
    )
    assert (
        not created.is_error
        and created.data["category_data"]["future_core_field"] == "preserved"
    ), created.data
    assert captured[1]["backup_id"] is None and captured[1]["current_config"] is None
    removed = await engine.call(
        "ha_config_remove_category",
        {"scope": "automation", "category_id": "cat"},
        CallContext("actor"),
    )
    assert not removed.is_error and ("automation", "cat") not in registry.rows, (
        removed.data
    )
    assert captured[2]["current_config"]["name"] == "Updated"
    assert [call[0] for call in registry.calls] == ["update", "create", "delete"]
    assert [payload["type"] for payload in validated] == [
        "config/category_registry/update",
        "config/category_registry/create",
        "config/category_registry/delete",
    ]


async def test_native_category_typed_concurrent_change_after_backup_is_not_overwritten(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        registry.rows[("automation", "cat")]["future_core_field"] = 0
        return {}

    engine, _, registry, _ = _category_setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_config_set_category",
        {"scope": "automation", "category_id": "cat", "name": "Replacement"},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    assert (
        registry.calls == []
        and registry.rows[("automation", "cat")]["name"] == "Before"
    )


@pytest.mark.parametrize("failure", ["invalid", "read_only", "unregistered"])
async def test_native_category_registered_validator_cannot_bypass_live_write_admission(
    native: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    engine, hass, registry, _ = _category_setup(native, monkeypatch)

    def validate(payload: dict) -> dict:
        if failure == "invalid":
            raise ValueError("Core rejected icon")
        engine.read_only = True
        return payload

    command = "config/category_registry/update"
    if failure == "unregistered":
        hass.data["websocket_api"].pop(command)
    else:
        hass.data["websocket_api"][command] = (None, validate)
    result = await engine.call(
        "ha_config_set_category",
        {"scope": "automation", "category_id": "cat", "name": "Replacement"},
        CallContext("actor"),
    )
    assert result.is_error and registry.calls == [], result.data
    assert registry.rows[("automation", "cat")]["name"] == "Before"


async def test_native_category_missing_and_cross_scope_targets_cannot_modify_another_category(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, registry, _ = _category_setup(native, monkeypatch)
    result = await engine.call(
        "ha_config_remove_category",
        {"scope": "script", "category_id": "cat"},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "RESOURCE_NOT_FOUND", (
        result.data
    )
    assert registry.calls == [] and ("automation", "cat") in registry.rows


async def test_remote_category_updates_compare_before_image_and_keep_the_ws_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False),
    )
    rows = {"cat": {"category_id": "cat", "name": "Before", "future_core_field": 42}}
    calls = []

    async def send(message: dict) -> dict:
        calls.append(copy.deepcopy(message))
        assert message["scope"] == "automation"
        if message["type"].endswith("/list"):
            return {"success": True, "result": copy.deepcopy(list(rows.values()))}
        assert message["type"] == "config/category_registry/update"
        rows[message["category_id"]].update(
            {
                key: value
                for key, value in message.items()
                if key not in {"type", "scope"}
            }
        )
        return {"success": True, "result": copy.deepcopy(rows[message["category_id"]])}

    client = SimpleNamespace(send_websocket_message=send)
    backend = RemoteBackend(client)
    definition = define_tool(
        backend,
        CategoryWriteTools,
        "ha_config_set_category",
        annotations={"readOnlyHint": False},
    )
    result = await Engine(backend, [definition]).call(
        "ha_config_set_category",
        {"scope": "automation", "category_id": "cat", "name": "Updated"},
        CallContext("remote"),
    )
    assert (
        not result.is_error and result.data["category_data"]["future_core_field"] == 42
    ), result.data
    assert [message["type"].rsplit("/", 1)[-1] for message in calls] == [
        "list",
        "list",
        "update",
    ]


@pytest.mark.parametrize("remote", [False, True])
async def test_category_semantic_write_cannot_override_the_transport_envelope(
    native: Any, monkeypatch: pytest.MonkeyPatch, remote: bool
) -> None:
    from ha_mcp.tool_api import ToolFailure

    engine, hass, registry, _ = _category_setup(native, monkeypatch)
    client = SimpleNamespace(send_websocket_message=AsyncMock())
    backend = RemoteBackend(client) if remote else engine.backend
    before = copy.deepcopy(registry.rows[("automation", "cat")])
    with pytest.raises(ToolFailure, match="envelope"):
        await backend.execute(
            "registry.category.update",
            {
                "data": {
                    "scope": "automation",
                    "category_id": "cat",
                    "name": "Forbidden",
                    "type": "unrelated/write",
                },
                "expected_item": before,
            },
            CallContext("actor"),
        )
    assert registry.calls == []
    client.send_websocket_message.assert_not_awaited()


@pytest.mark.parametrize("action", ["create", "update", "delete"])
async def test_native_category_mutate_then_raise_reports_unknown_outcome_without_replaying(
    native: Any, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    engine, _, registry, _ = _category_setup(native, monkeypatch)
    original = getattr(registry, f"async_{action}")

    def mutate_then_raise(**arguments: Any) -> Any:
        original(**arguments)
        raise ValueError("Listener failed after registry changed")

    monkeypatch.setattr(registry, f"async_{action}", mutate_then_raise)
    arguments = (
        {"scope": "automation", "category_id": "cat"}
        if action != "create"
        else {"scope": "automation"}
    )
    if action != "delete":
        arguments["name"] = "After"
    name = (
        "ha_config_remove_category" if action == "delete" else "ha_config_set_category"
    )
    result = await engine.call(name, arguments, CallContext("actor"))
    assert result.is_error and result.data["error"]["code"] == "OUTCOME_UNKNOWN", (
        result.data
    )
    assert result.status == "outcome_unknown" and len(registry.calls) == 1


async def test_native_category_owned_name_validator_can_reject_before_dispatch(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, registry, _ = _category_setup(native, monkeypatch)
    checked = []

    def reject_name(*arguments: Any) -> None:
        checked.append(arguments)
        raise ValueError("Core says this name is already in use")

    monkeypatch.setattr(
        registry, "_async_ensure_name_is_available", reject_name, raising=False
    )
    result = await engine.call(
        "ha_config_set_category",
        {"scope": "automation", "name": "Taken"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert checked == [("automation", "Taken")] and registry.calls == []


async def test_native_category_serialization_failure_after_create_is_not_a_rejected_create(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, registry, _ = _category_setup(native, monkeypatch)

    def serialize(entry: dict) -> dict:
        raise ValueError("Serializer failed after the Core owner returned")

    monkeypatch.setattr(
        sys.modules["homeassistant.components.config.category_registry"],
        "_entry_dict",
        serialize,
    )
    result = await engine.call(
        "ha_config_set_category",
        {"scope": "automation", "name": "New"},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "OUTCOME_UNKNOWN", (
        result.data
    )
    assert ("automation", "new") in registry.rows and len(registry.calls) == 1


def test_legacy_registry_error_seam_preserves_classified_code_and_execution_status() -> (
    None
):
    from ha_mcp.tool_api import ToolFailure
    from ha_mcp.tools.tools_categories import CategoryTools

    failure = ToolFailure("CONFIG_CONFLICT", "Do not dispatch", status="not_started")
    with pytest.raises(ToolFailure) as caught:
        CategoryTools._map_category_write_error(failure, {}, [])
    assert caught.value is failure and caught.value.status == "not_started"
