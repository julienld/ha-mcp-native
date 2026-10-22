"""Area/floor writes preserve clear semantics, snapshots and label guarantees."""

import copy
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.backends.remote import RemoteBackend
from ha_mcp.runtime import Engine
from ha_mcp.runtime.area_writes import AreaWriteTools
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.tool_api import CallContext

from .test_component_ws_search import (
    FakeArea,
    FakeFloor,
    FakeHass,
    FakeLabel,
    make_view,
    wsapi,
)
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


def _serialize(entry: Any) -> dict:
    return {
        ("area_id" if key == "id" else key): sorted(value)
        if isinstance(value, set)
        else copy.deepcopy(value)
        for key, value in vars(entry).items()
    }


class _Area(FakeArea):
    @property
    def json_fragment(self) -> dict:
        return _serialize(self)


class _Registry:
    def __init__(self, domain: str) -> None:
        self.domain = domain
        entry = (
            _Area("existing", "Before")
            if domain == "area"
            else FakeFloor("existing", "Before")
        )
        entry.future_core_field = False
        self.entries = {"existing": entry}
        self.calls = []

    def async_get_area(self, identifier: str) -> Any:
        return self.entries.get(identifier)

    async_get_floor = async_get_area

    def async_list_areas(self) -> list:
        return list(self.entries.values())

    async_list_floors = async_list_areas

    def async_create(self, **data: Any) -> Any:
        entry = (
            _Area("new", **data) if self.domain == "area" else FakeFloor("new", **data)
        )
        entry.future_core_field = "kept"
        self.entries["new"] = entry
        self.calls.append(("create", copy.deepcopy(data)))
        return entry

    def async_update(self, **data: Any) -> Any:
        identifier = data.pop(f"{self.domain}_id")
        if identifier not in self.entries:
            raise ValueError("ID doesn't exist")
        entry = self.entries[identifier]
        for key, value in data.items():
            setattr(entry, key, value)
        self.calls.append(("update", copy.deepcopy(data)))
        return entry

    def async_delete(self, **data: Any) -> None:
        del self.entries[data[f"{self.domain}_id"]]
        self.calls.append(("delete", copy.deepcopy(data)))


def _setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    hass = FakeHass()
    authorize(hass)
    areas, floors = _Registry("area"), _Registry("floor")
    view = make_view(labels=[FakeLabel("valid", "Valid")])
    view.area, view.floor = areas, floors
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    for domain, registry in (("area", areas), ("floor", floors)):
        owner = SimpleNamespace(async_get=lambda hass, registry=registry: registry)
        monkeypatch.setattr(
            sys.modules["homeassistant.helpers"],
            f"{domain}_registry",
            owner,
            raising=False,
        )
        monkeypatch.setitem(
            sys.modules, f"homeassistant.helpers.{domain}_registry", owner
        )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.config.floor_registry",
        SimpleNamespace(_entry_dict=_serialize),
    )
    labels = SimpleNamespace(
        async_get_missing_label_ids=lambda hass, labels: labels - {"valid"}
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "label_registry", labels, raising=False
    )
    schemas = []

    def validate(payload: dict) -> dict:
        schemas.append(copy.deepcopy(payload))
        return payload

    hass.data["websocket_api"] = {
        f"config/{domain}_registry/{action}": (None, validate)
        for domain in ("area", "floor")
        for action in ("create", "update", "delete")
    }
    backend = native.NativeBackend(
        hass, registry_backup=backup or AsyncMock(return_value={})
    )
    definitions = [
        define_tool(backend, AreaWriteTools, name, annotations={"readOnlyHint": False})
        for name in ("ha_set_area_or_floor", "ha_remove_area_or_floor")
    ]
    return Engine(backend, definitions), hass, areas, floors, schemas


@pytest.mark.parametrize("kind", ["area", "floor"])
async def test_native_area_floor_create_update_remove_preserve_legacy_snapshots_and_owner_fields(
    native: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    captured = []

    async def backup(payload: dict, actor: CallContext) -> dict:
        captured.append(copy.deepcopy(payload))
        return {}

    engine, _, areas, floors, schemas = _setup(native, monkeypatch, backup=backup)
    registry = areas if kind == "area" else floors
    before = _serialize(registry.entries["existing"])
    result = await engine.call(
        "ha_set_area_or_floor",
        {
            "kind": kind,
            "id": "existing",
            "name": "After",
            "aliases": [" Kitchen ", "", "Kitchen"],
            "icon": None,
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data[kind]["future_core_field"] is False and result.data[kind][
        "aliases"
    ] == ["Kitchen"]
    assert (
        registry.calls[0][1]["aliases"] == {"Kitchen"}
        and registry.calls[0][1]["icon"] is None
    )
    assert (
        captured[0]["domain"] == "area_or_floor"
        and captured[0]["backup_id"] == f"{kind}:existing"
    )
    assert captured[0]["current_config"] == {"kind": kind, **before}
    created = await engine.call(
        "ha_set_area_or_floor", {"kind": kind, "name": "New"}, CallContext("actor")
    )
    assert not created.is_error and created.data[kind]["future_core_field"] == "kept", (
        created.data
    )
    assert captured[1]["current_config"] is None
    deleted = await engine.call(
        "ha_remove_area_or_floor",
        {"kind": kind, "id": "existing"},
        CallContext("actor"),
    )
    assert not deleted.is_error and "existing" not in registry.entries, deleted.data
    assert [call[0] for call in registry.calls] == ["update", "create", "delete"]
    assert len(schemas) == 3


async def test_native_area_labels_and_floor_are_validated_before_the_write(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, areas, _, _ = _setup(native, monkeypatch)
    bad = await engine.call(
        "ha_set_area_or_floor",
        {"kind": "area", "id": "existing", "labels": ["missing"]},
        CallContext("actor"),
    )
    assert bad.is_error and not areas.calls, bad.data
    assigned = await engine.call(
        "ha_set_area_or_floor",
        {"kind": "area", "id": "existing", "labels": ["valid"], "floor_id": "existing"},
        CallContext("actor"),
    )
    assert not assigned.is_error, assigned.data
    assert (
        assigned.data["area"]["labels"] == ["valid"]
        and assigned.data["area"]["floor_id"] == "existing"
    )
    assert areas.calls[0][1]["labels"] == {"valid"}
    cleared = await engine.call(
        "ha_set_area_or_floor",
        {"kind": "area", "id": "existing", "labels": [], "floor_id": None},
        CallContext("actor"),
    )
    assert (
        not cleared.is_error
        and cleared.data["area"]["labels"] == []
        and cleared.data["area"]["floor_id"] is None
    ), cleared.data


async def test_native_area_label_deleted_during_snapshot_reports_write_committed_and_never_retries_create(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        labels.async_get_missing_label_ids = lambda hass, wanted: wanted
        return {}

    engine, _, areas, _, _ = _setup(native, monkeypatch, backup=backup)
    labels = sys.modules["homeassistant.helpers"].label_registry
    result = await engine.call(
        "ha_set_area_or_floor",
        {"kind": "area", "name": "Created", "labels": ["valid"]},
        CallContext("actor"),
    )
    assert result.is_error and result.data.get("write_committed") is True, result.data
    assert (
        len(areas.calls) == 1
        and areas.calls[0][0] == "create"
        and "new" in areas.entries
    )
    assert areas.entries["new"].labels == set()


async def test_native_area_concurrent_typed_change_after_snapshot_is_preserved(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        areas.entries["existing"].future_core_field = 0
        return {}

    engine, _, areas, _, _ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_set_area_or_floor",
        {"kind": "area", "id": "existing", "name": "Replacement"},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    assert areas.calls == [] and areas.entries["existing"].name == "Before"


async def test_remote_area_floor_write_keeps_explicit_clear_and_existing_ws_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=False),
    )
    row = {
        "area_id": "existing",
        "name": "Before",
        "icon": "mdi:home",
        "future_core_field": True,
    }
    calls = []

    async def send(message: dict) -> dict:
        calls.append(copy.deepcopy(message))
        if message["type"] == "config/area_registry/list":
            return {"success": True, "result": [copy.deepcopy(row)]}
        assert message["type"] == "config/area_registry/update"
        row.update({key: value for key, value in message.items() if key != "type"})
        return {"success": True, "result": copy.deepcopy(row)}

    backend = RemoteBackend(SimpleNamespace(send_websocket_message=send))
    definition = define_tool(
        backend,
        AreaWriteTools,
        "ha_set_area_or_floor",
        annotations={"readOnlyHint": False},
    )
    result = await Engine(backend, [definition]).call(
        "ha_set_area_or_floor",
        {"kind": "area", "id": "existing", "icon": None},
        CallContext("remote"),
    )
    assert (
        not result.is_error
        and result.data["area"]["icon"] is None
        and result.data["area"]["future_core_field"] is True
    ), result.data
    assert calls[-1] == {
        "type": "config/area_registry/update",
        "area_id": "existing",
        "icon": None,
    }


@pytest.mark.parametrize("kind", ["area", "floor"])
@pytest.mark.parametrize("action", ["create", "update", "delete"])
async def test_native_area_floor_mutate_then_raise_reports_unknown_outcome_without_replaying(
    native: Any, monkeypatch: pytest.MonkeyPatch, kind: str, action: str
) -> None:
    engine, _, areas, floors, _ = _setup(native, monkeypatch)
    registry = areas if kind == "area" else floors
    original = getattr(registry, f"async_{action}")

    def mutate_then_raise(**arguments: Any) -> Any:
        original(**arguments)
        raise (
            KeyError("Listener failed after registry changed")
            if action == "delete"
            else ValueError("Listener failed after registry changed")
        )

    monkeypatch.setattr(registry, f"async_{action}", mutate_then_raise)
    arguments = (
        {"kind": kind, "id": "existing"} if action != "create" else {"kind": kind}
    )
    if action != "delete":
        arguments["name"] = "After"
    name = "ha_remove_area_or_floor" if action == "delete" else "ha_set_area_or_floor"
    result = await engine.call(name, arguments, CallContext("actor"))
    assert result.is_error and result.data["error"]["code"] == "OUTCOME_UNKNOWN", (
        result.data
    )
    assert result.status == "outcome_unknown" and len(registry.calls) == 1


async def test_native_area_unchanged_state_after_dispatched_noop_does_not_prove_rejection(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, areas, _, _ = _setup(native, monkeypatch)
    before = _serialize(areas.entries["existing"])
    original = areas.async_update

    def noop_then_raise(**arguments: Any) -> Any:
        original(**arguments)
        raise ValueError("Core emitted its update event and its listener failed")

    monkeypatch.setattr(areas, "async_update", noop_then_raise)
    result = await engine.call(
        "ha_set_area_or_floor",
        {"kind": "area", "id": "existing", "name": "Before"},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "OUTCOME_UNKNOWN", (
        result.data
    )
    assert _serialize(areas.entries["existing"]) == before and len(areas.calls) == 1
