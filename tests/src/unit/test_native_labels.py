"""Canonical label tools call Core directly and retain exact pre-write ownership."""

import asyncio
import copy
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_component_ws_search import FakeHass, FakeLabel
from .test_native_catalog_contract import (
    _ANNOTATIONS,
    _changed_inputs,
    _structural_schema,
)
from .test_native_catalog_contract_area_writes import _Area, _Registry
from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native

_STAMP = datetime(2026, 1, 1, tzinfo=UTC)
_CONTRACT = (
    Path(__file__).resolve().parents[3] / "docs/native-migration/catalog-baseline.json"
)


def _serialized(entry: Any) -> dict[str, Any]:
    return {
        key: value.timestamp() if isinstance(value, datetime) else copy.deepcopy(value)
        for key, value in vars(entry).items()
    }


class _Labels:
    def __init__(self) -> None:
        self.rows = {}
        self.calls = []
        self.sequence = 1
        for key in ("existing", "other"):
            self.rows[key] = self.entry(key, key.title())

    def entry(self, key: str, name: str, **data: Any) -> Any:
        self.sequence += 1
        entry = FakeLabel(
            key,
            name,
            created_at=_STAMP + timedelta(seconds=self.sequence),
            modified_at=_STAMP + timedelta(seconds=self.sequence),
            **data,
        )
        entry.future_core_field = False
        return entry

    def async_get_label(self, label_id: str) -> Any:
        return self.rows.get(label_id)

    def async_list_labels(self) -> list:
        return list(self.rows.values())

    def async_create(self, name: str, **data: Any) -> Any:
        key = name.casefold().replace(" ", "_")
        entry = self.entry(key, name, **data)
        self.rows[key] = entry
        self.calls.append(("create", {"name": name, **copy.deepcopy(data)}))
        return entry

    def async_update(self, label_id: str, **data: Any) -> Any:
        entry = self.rows[label_id]
        for key, value in data.items():
            setattr(entry, key, value)
        self.calls.append(("update", {"label_id": label_id, **copy.deepcopy(data)}))
        return entry

    def async_delete(self, label_id: str) -> None:
        del self.rows[label_id]
        self.calls.append(("delete", {"label_id": label_id}))


def _setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    hass = FakeHass()
    user = authorize(hass)
    labels, areas = _Labels(), _Registry("area")
    areas.entries["existing"].labels = {"other"}
    areas.entries["second"] = _Area("second", "Second")
    areas.entries["already"] = _Area("already", "Already", labels={"existing"})
    label_owner = SimpleNamespace(
        async_get=lambda hass: labels,
        async_get_missing_label_ids=lambda hass, requested: {
            value for value in requested if labels.async_get_label(value) is None
        },
    )
    area_owner = SimpleNamespace(async_get=lambda hass: areas)
    for name, module in (("label", label_owner), ("area", area_owner)):
        monkeypatch.setattr(
            sys.modules["homeassistant.helpers"],
            f"{name}_registry",
            module,
            raising=False,
        )
        monkeypatch.setitem(
            sys.modules, f"homeassistant.helpers.{name}_registry", module
        )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.config.label_registry",
        SimpleNamespace(_entry_dict=_serialized),
    )
    schemas = []

    def validate(message: dict) -> dict:
        schemas.append(copy.deepcopy(message))
        if isinstance(message.get("color"), str):
            message["color"] = message["color"].lower()
        return message

    hass.data["websocket_api"] = {
        f"config/{kind}_registry/{action}": (None, validate)
        for kind in ("label", "area")
        for action in ("create", "update", "delete")
    }
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.labels"
    )
    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(
        access, "json_bytes", sys.modules["homeassistant.helpers.json"].json_bytes
    )
    backend = native.NativeBackend(hass)
    backend.execute = AsyncMock(
        side_effect=AssertionError(
            "A canonical label tool must not use an operation dispatcher"
        )
    )
    manager = SimpleNamespace(
        hass=hass,
        backend=backend,
        snapshots=SimpleNamespace(registry_backup=backup or AsyncMock(return_value={})),
    )
    bindings = importlib.import_module(
        "custom_components.ha_mcp_tools.native_label_tools"
    )
    engine = Engine(backend, bindings.build_label_tools(manager))
    return engine, manager, labels, areas, schemas, user, owner


async def test_native_label_get_set_remove_use_core_schema_and_preserve_canonical_fields(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, manager, labels, _, schemas, _, _ = _setup(native, monkeypatch)
    created = await engine.call(
        "ha_config_set_label",
        {"name": "New Label", "color": "#AABBCC"},
        CallContext("actor"),
    )
    assert not created.is_error, created.data
    assert (
        created.data["label_id"] == "new_label"
        and created.data["label_data"]["future_core_field"] is False
    )
    assert labels.calls[0][1]["color"] == "#aabbcc"
    fetched = await engine.call(
        "ha_config_get_label",
        {"label_id": "new_label"},
        CallContext("actor", read_only=True),
    )
    assert (
        not fetched.is_error and fetched.data["label"]["future_core_field"] is False
    ), fetched.data
    removed = await engine.call(
        "ha_config_remove_label", {"label_id": "new_label"}, CallContext("actor")
    )
    assert not removed.is_error and "new_label" not in labels.rows, removed.data
    assert [call[0] for call in labels.calls] == ["create", "delete"]
    assert [row["type"] for row in schemas] == [
        "config/label_registry/create",
        "config/label_registry/delete",
    ]
    assert all("type" not in data and "id" not in data for _, data in labels.calls)
    manager.backend.execute.assert_not_awaited()


async def test_native_label_adds_to_latest_area_sets_and_captures_only_changed_targets(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    async def backup(payload: dict, actor: CallContext) -> dict:
        assert actor.user_id == "actor"
        captured.append(copy.deepcopy(payload))
        return {"warnings": ["Captured " + payload["backup_id"]]}

    engine, _, labels, areas, _, _, _ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_config_set_label",
        {
            "name": "Existing",
            "label_id": "existing",
            "areas": ["existing", "already", "second", "existing"],
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert result.data["assigned_areas"] == ["existing", "already", "second"]
    assert areas.entries["existing"].labels == {"other", "existing"} and areas.entries[
        "second"
    ].labels == {"existing"}
    assert [row["backup_id"] for row in captured] == [
        "existing",
        "area:existing",
        "area:second",
    ]
    assert (
        captured[1]["current_config"]["labels"] == ["other"]
        and captured[1]["current_config"]["kind"] == "area"
    )
    assert all(row["source_tool"] == "ha_config_set_label" for row in captured)
    assert (
        len(result.data["warnings"]) == 3
        and len(areas.calls) == 2
        and len(labels.calls) == 1
    )


async def test_native_label_unknown_areas_are_rejected_before_creating_a_label(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, labels, areas, _, _, _ = _setup(native, monkeypatch)
    result = await engine.call(
        "ha_config_set_label",
        {"name": "New", "areas": ["missing"]},
        CallContext("actor"),
    )
    assert (
        result.is_error
        and result.data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    ), result.data
    assert labels.calls == areas.calls == [] and result.status == "not_started"


@pytest.mark.parametrize("phase", ["label", "area"])
async def test_native_label_rechecks_revocation_after_each_capture_without_claiming_a_saved_label_was_rejected(
    native: Any, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        if payload["domain"] == ("label" if phase == "label" else "area_or_floor"):
            user.is_active = False
        return {}

    engine, _, labels, areas, _, user, _ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_config_set_label",
        {"name": "Existing", "label_id": "existing", "areas": ["existing"]},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "AUTH_FORBIDDEN", (
        result.data
    )
    assert not areas.calls
    assert len(labels.calls) == (0 if phase == "label" else 1)
    assert result.status == ("not_started" if phase == "label" else "confirmed")
    if phase == "area":
        assert (
            result.data["partial"] is True
            and result.data["label_applied"] is True
            and result.data["assigned_areas"] == []
        )


@pytest.mark.parametrize("phase", ["label", "area"])
async def test_native_label_typed_conflicts_preserve_concurrent_edits(
    native: Any, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        if payload["domain"] == ("label" if phase == "label" else "area_or_floor"):
            target = (
                labels.rows["existing"]
                if phase == "label"
                else areas.entries["existing"]
            )
            target.future_core_field = 0
        return {}

    engine, _, labels, areas, _, _, _ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_config_set_label",
        {"name": "Existing", "label_id": "existing", "areas": ["existing"]},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    assert not areas.calls and len(labels.calls) == (0 if phase == "label" else 1)
    assert result.status == ("not_started" if phase == "label" else "confirmed")


async def test_native_label_reused_slug_after_capture_cannot_assign_a_replacement_label(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        if payload["domain"] == "area_or_floor":
            labels.rows["existing"] = labels.entry("existing", "Replacement")
        return {}

    engine, _, labels, areas, _, _, _ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_config_set_label",
        {"name": "Existing", "label_id": "existing", "areas": ["existing"]},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    assert result.data["label_applied"] is True and result.status == "confirmed"
    assert not areas.calls and labels.rows["existing"].name == "Replacement"


@pytest.mark.parametrize("action", ["create", "update", "delete"])
async def test_native_label_callback_mutate_then_raise_is_unknown_and_never_replayed(
    native: Any, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    engine, _, labels, _, _, _, _ = _setup(native, monkeypatch)
    original = getattr(labels, f"async_{action}")

    def change_then_raise(**data: Any) -> Any:
        original(**data)
        raise ValueError("Save/listener failed after the mutation")

    monkeypatch.setattr(labels, f"async_{action}", change_then_raise)
    arguments = {"label_id": "existing"} if action != "create" else {}
    if action != "delete":
        arguments["name"] = "New"
    result = await engine.call(
        "ha_config_remove_label" if action == "delete" else "ha_config_set_label",
        arguments,
        CallContext("actor"),
    )
    assert (
        result.is_error
        and result.data["error"]["code"] == "OUTCOME_UNKNOWN"
        and result.status == "outcome_unknown"
    ), result.data
    assert len(labels.calls) == 1


async def test_native_label_partial_area_callback_failure_keeps_known_progress_and_unknown_outcome(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, labels, areas, _, _, _ = _setup(native, monkeypatch)
    original = areas.async_update

    def change_then_raise(**data: Any) -> Any:
        result = original(**data)
        if data["area_id"] == "second":
            raise ValueError("Listener failed after second area changed")
        return result

    monkeypatch.setattr(areas, "async_update", change_then_raise)
    result = await engine.call(
        "ha_config_set_label",
        {"name": "Existing", "label_id": "existing", "areas": ["existing", "second"]},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert result.data["label_applied"] is True and result.data["assigned_areas"] == [
        "existing"
    ]
    assert (
        result.data["area_id"] == "second"
        and result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    )
    assert len(labels.calls) == 1 and len(areas.calls) == 2


async def test_native_label_cancellation_during_area_capture_does_not_dispatch_the_area(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def backup(payload: dict, actor: CallContext) -> dict:
        if payload["domain"] == "area_or_floor":
            entered.set()
            await release.wait()
        return {}

    engine, _, labels, areas, _, _, _ = _setup(native, monkeypatch, backup=backup)
    pending = asyncio.create_task(
        engine.call(
            "ha_config_set_label",
            {"name": "Existing", "label_id": "existing", "areas": ["existing"]},
            CallContext("actor"),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert len(labels.calls) == 1 and areas.calls == []


async def test_native_label_restore_reuses_core_validation_and_cas_without_a_second_capture(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = AsyncMock(return_value={})
    _, manager, labels, _, _, _, owner = _setup(native, monkeypatch, backup=backup)
    actor = CallContext("actor")
    service = owner.NativeLabels(
        manager.hass, actor, authorize=manager.backend.authorize, backup=backup
    )
    before = await service.read_snapshot("existing")
    saved = {**before, "name": "Restored"}
    result = await Engine(manager.backend).call_owned(
        "restore-label",
        lambda: service.restore_snapshot("existing", saved, before),
        actor,
        writes=True,
    )
    assert not result.is_error and labels.rows["existing"].name == "Restored", (
        result.data
    )
    backup.assert_not_awaited()
    before = await service.read_snapshot("existing")
    labels.rows["existing"].name = "Concurrent edit"
    refused = await Engine(manager.backend).call_owned(
        "restore-label",
        lambda: service.restore_snapshot("existing", saved, before),
        actor,
        writes=True,
    )
    assert (
        refused.is_error
        and refused.data["error"]["code"] == "CONFIG_CONFLICT"
        and labels.rows["existing"].name == "Concurrent edit"
    )


@pytest.fixture
def label_contract() -> dict[str, Any]:
    baseline = {
        tool["name"]: tool for tool in json.loads(_CONTRACT.read_text())["tools"]
    }
    baseline.update(
        {
            tool["name"]: tool
            for tool in json.loads(
                (_CONTRACT.parent / "catalog-extras-baseline.json").read_text()
            )["tools"]
        }
    )
    overrides = json.loads((_CONTRACT.parent / "compatibility.json").read_text())[
        "native_annotation_overrides"
    ]
    for name, override in overrides.items():
        baseline[name]["annotations"].update(override["annotations"])
    return baseline


async def test_component_catalog_has_one_label_getter_and_preserves_immutable_contracts(
    native: Any, monkeypatch: pytest.MonkeyPatch, label_contract: dict[str, Any]
) -> None:
    _, manager, _, _, _, _, _ = _setup(native, monkeypatch)
    catalog = importlib.import_module(
        "custom_components.ha_mcp_tools.native_catalog"
    ).build_native_catalog(manager)
    names = [tool.name for tool in catalog]
    assert names.count("ha_config_get_label") == 1 and len(names) == len(set(names))
    assert {
        "ha_config_get_label",
        "ha_config_set_label",
        "ha_config_remove_label",
        "ha_eval_template",
        "ha_get_skill_guide",
    } <= set(names)
    for definition in catalog:
        if definition.name not in label_contract:
            continue
        previous = label_contract[definition.name]
        assert not _changed_inputs(previous["inputSchema"], definition.input_schema)
        assert all(
            previous["annotations"].get(key) == definition.annotations.get(key)
            for key in _ANNOTATIONS
        )
        assert _structural_schema(previous.get("outputSchema")) == _structural_schema(
            definition.output_schema
        )
        assert definition.tags == set(previous["_meta"]["fastmcp"]["tags"])


async def test_native_label_area_recreated_during_capture_is_not_modified(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        if payload["domain"] == "area_or_floor":
            replacement = _Area("existing", "Before", labels={"other"}, created_at=2.0)
            replacement.future_core_field = False
            areas.entries["existing"] = replacement
        return {}

    engine, _, _, areas, _, _, _ = _setup(native, monkeypatch, backup=backup)
    areas.entries["existing"].created_at = 1.0
    result = await engine.call(
        "ha_config_set_label",
        {"name": "Existing", "label_id": "existing", "areas": ["existing"]},
        CallContext("actor"),
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    assert result.status == "confirmed" and result.data["label_applied"] is True
    assert areas.calls == [] and areas.entries["existing"].labels == {"other"}


async def test_native_label_removed_by_core_listener_after_area_update_is_not_reported_as_assigned(
    native: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, labels, areas, _, _, _ = _setup(native, monkeypatch)
    original = areas.async_update

    def update_then_remove_label(**data: Any) -> Any:
        written = original(**data)
        labels.rows.pop("existing")
        written.labels.discard("existing")
        return written

    monkeypatch.setattr(areas, "async_update", update_then_remove_label)
    result = await engine.call(
        "ha_config_set_label",
        {"name": "Existing", "label_id": "existing", "areas": ["existing"]},
        CallContext("actor"),
    )
    assert (
        result.is_error
        and result.data["partial"] is True
        and result.status == "confirmed"
    ), result.data
    assert (
        result.data["assigned_areas"] == [] and result.data["write_committed"] is True
    )
    assert len(areas.calls) == 1 and areas.entries["existing"].labels == {"other"}


async def test_native_label_assignments_yield_to_queued_policy_changes_between_areas(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _, areas, _, _, _ = _setup(native, monkeypatch)
    update = areas.async_update

    def update_then_queue_disable(**data: Any) -> Any:
        entry = update(**data)
        asyncio.get_running_loop().call_soon(setattr, engine, "enabled_tools", set())
        return entry

    monkeypatch.setattr(areas, "async_update", update_then_queue_disable)
    result = await engine.call(
        "ha_config_set_label",
        {"label_id": "existing", "name": "Existing", "areas": ["existing", "second"]},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "confirmed", result.data
    assert result.data["label_applied"] is True and result.data["assigned_areas"] == [
        "existing"
    ]
    assert "existing" not in areas.entries["second"].labels and len(areas.calls) == 1
