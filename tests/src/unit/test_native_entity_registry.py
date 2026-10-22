"""Native entity removal and registry history never impersonate state writes."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_devices import _Devices
from .test_native_devices import _setup as device_setup
from .test_native_engine_backend import native as native

_COMPUTED = "<core-computed-name>"


class _Entity(SimpleNamespace):
    @property
    def extended_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(vars(self))
        result["labels"] = sorted(self.labels)
        result["aliases"] = [
            None if alias == _COMPUTED else alias for alias in self.aliases
        ]
        return result


class _Registry:
    def __init__(self) -> None:
        self.rows = {
            f"sensor.{name}": self.new(f"sensor.{name}", f"uuid-{name}")
            for name in ("first", "second")
        }
        self.calls = []

    @staticmethod
    def new(entity_id: str, identity: str) -> Any:
        return _Entity(
            entity_id=entity_id,
            id=identity,
            platform="example",
            unique_id=identity,
            config_entry_id="entry",
            created_at=1.0,
            modified_at=1.0,
            name="Before",
            icon=None,
            device_class=None,
            disabled_by=None,
            hidden_by=None,
            device_id=None,
            area_id=None,
            categories={},
            labels={"other"},
            aliases=[_COMPUTED, "Before alias"],
            options={"sensor": {"precision": 1}},
            future_core_field=False,
        )

    def async_get(self, entity_id: str) -> Any:
        return self.rows.get(entity_id) or next(
            (row for row in self.rows.values() if row.id == entity_id), None
        )

    async_remove = _Devices.async_remove_device

    def async_update_entity(self, entity_id: str, **changes: Any) -> Any:
        previous = self.rows.pop(entity_id)
        new_id = changes.pop("new_entity_id", entity_id)
        row = _Entity(
            **{**vars(previous), **copy.deepcopy(changes), "entity_id": new_id}
        )
        self.rows[new_id] = row
        self.calls.append(("update", entity_id, copy.deepcopy(changes)))
        return row

    def async_update_entity_options(
        self, entity_id: str, domain: str, value: Any
    ) -> Any:
        row = _Entity(**copy.deepcopy(vars(self.rows[entity_id])))
        if value is None:
            row.options.pop(domain, None)
        else:
            row.options[domain] = copy.deepcopy(value)
        self.rows[entity_id] = row
        self.calls.append(("options", entity_id, domain, value))
        return row


def _setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    _, manager, *_rest = device_setup(native, monkeypatch, backup=backup)
    registry = _Registry()
    er = SimpleNamespace(async_get=lambda hass: registry, COMPUTED_NAME=_COMPUTED)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", er)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er)
    category = SimpleNamespace(
        async_get=lambda hass: SimpleNamespace(
            async_list_categories=lambda *, scope: []
        )
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"],
        "category_registry",
        category,
        raising=False,
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.category_registry", category
    )
    schemas = []

    def validate(payload: dict) -> dict:
        schemas.append(copy.deepcopy(payload))
        payload["entity_id"] = payload["entity_id"].lower()
        return payload

    manager.hass.data["websocket_api"].update(
        {
            f"config/entity_registry/{action}": (None, validate)
            for action in ("remove", "update")
        }
    )
    bindings = importlib.import_module(
        "custom_components.ha_mcp_tools.native_entity_tools"
    )
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.entity_registry"
    )
    engine = Engine(manager.backend, [bindings.entity_removal_definition(manager)])
    return engine, manager, registry, schemas, _rest[3], owner


async def test_entity_removal_captures_the_real_full_registry_row_under_its_uuid(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = AsyncMock(return_value={"warnings": ["capture note"]})
    engine, manager, registry, schemas, _, _ = _setup(
        native, monkeypatch, backup=backup
    )
    before = registry.rows["sensor.first"].extended_dict
    result = await engine.call(
        "ha_remove_entity", {"entity_id": "SENSOR.FIRST"}, CallContext("actor")
    )
    assert not result.is_error and "sensor.first" not in registry.rows, result.data
    assert registry.calls == [("remove", "sensor.first")]
    payload, actor = backup.call_args.args
    assert payload == {
        "domain": "entity_registry",
        "source_tool": "ha_remove_entity",
        "backup_id": "uuid-first",
        "current_config": before,
    }
    assert actor.user_id == "actor" and result.data["warnings"] == ["capture note"]
    assert schemas[0]["type"] == "config/entity_registry/remove"
    manager.backend.execute.assert_not_awaited()


async def test_bulk_entity_removal_classifies_absent_and_duplicate_targets_without_fake_captures(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = AsyncMock(return_value={})
    engine, _, registry, _, _, _ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_remove_entity",
        {
            "entity_id": [
                "sensor.first",
                "sensor.absent",
                "sensor.second",
                "sensor.first",
            ]
        },
        CallContext("actor"),
    )
    assert not result.is_error and result.data["removed"] == [
        "sensor.first",
        "sensor.second",
    ], result.data
    assert result.data["skipped"] == ["sensor.absent", "sensor.first"]
    assert len(registry.calls) == backup.await_count == 2
    assert {call.args[0]["backup_id"] for call in backup.call_args_list} == {
        "uuid-first",
        "uuid-second",
    }


async def test_unknown_entity_removal_keeps_prior_progress_and_does_not_replay_or_continue(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, _, _, _ = _setup(native, monkeypatch)
    remove = registry.async_remove

    def mutate_then_raise(entity_id: str) -> None:
        remove(entity_id)
        if entity_id == "sensor.second":
            raise ValueError("save failed after mutation")

    monkeypatch.setattr(registry, "async_remove", mutate_then_raise)
    result = await engine.call(
        "ha_remove_entity",
        {"entity_id": ["sensor.first", "sensor.second", "sensor.second"]},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert result.data["removed"] == ["sensor.first"]
    assert result.data["errors"][0]["code"] == "OUTCOME_UNKNOWN"
    assert (
        result.data["not_attempted"] == ["sensor.second"] and len(registry.calls) == 2
    )


@pytest.mark.parametrize("change", ["typed_edit", "rename", "recreate", "revoke"])
async def test_entity_removal_rechecks_exact_image_and_actor_after_capture(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        if change == "typed_edit":
            registry.rows["sensor.first"].future_core_field = 0
        elif change == "rename":
            row = registry.rows.pop("sensor.first")
            row.entity_id = "sensor.renamed"
            registry.rows[row.entity_id] = row
        elif change == "recreate":
            registry.rows["sensor.first"] = registry.new(
                "sensor.first", "different-uuid"
            )
        else:
            user.is_admin = False
        return {}

    engine, _, registry, _, user, _ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        "ha_remove_entity", {"entity_id": "sensor.first"}, CallContext("actor")
    )
    assert (
        result.is_error and result.status == "not_started" and registry.calls == []
    ), result.data


@pytest.mark.parametrize("stored", [True, False, None])
async def test_entity_removal_keeps_the_storage_helper_warning_and_yaml_distinction(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    stored: bool | None,
) -> None:
    engine, _, registry, _, _, owner = _setup(native, monkeypatch)
    registry.rows["sensor.first"].platform = "input_boolean"
    registry.rows["sensor.first"].unique_id = "stored"
    collection = SimpleNamespace(
        storage_collection=SimpleNamespace(
            data={"stored": {"id": "stored"}} if stored else {}
        )
    )
    monkeypatch.setattr(
        owner, "collection_owner", lambda *_: collection if stored is not None else None
    )
    result = await engine.call(
        "ha_remove_entity", {"entity_id": "sensor.first"}, CallContext("actor")
    )
    assert not result.is_error, result.data
    if stored:
        assert "ha_remove_helpers_integrations" in result.data["warnings"][0]
    elif stored is None:
        assert "could not be read" in result.data["warnings"][0]
    else:
        assert "warnings" not in result.data


async def test_entity_restore_follows_uuid_after_rename_and_uses_core_aliases_and_options(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = AsyncMock()
    _, manager, registry, schemas, _, owner = _setup(native, monkeypatch, backup=backup)
    saved = registry.rows["sensor.first"].extended_dict
    row = registry.rows.pop("sensor.first")
    row.entity_id, row.name, row.aliases = "sensor.renamed", "After", ["After alias"]
    row.options["added"] = {"value": True}
    registry.rows[row.entity_id] = row
    service = owner.NativeEntityRegistry(
        manager.hass,
        CallContext("actor"),
        authorize=manager.backend.authorize,
        backup=backup,
    )
    expected = await service.read_snapshot("uuid-first")
    result = await Engine(manager.backend).call_owned(
        "registry-restore",
        lambda: service.restore_snapshot("uuid-first", saved, expected),
        CallContext("actor"),
        writes=True,
    )
    assert (
        not result.is_error and registry.rows["sensor.first"].extended_dict == saved
    ), result.data
    assert registry.rows["sensor.first"].aliases == [_COMPUTED, "Before alias"]
    assert any(
        payload.get("options_domain") == "added" and payload["options"] is None
        for payload in schemas
    )
    assert "sensor.renamed" not in registry.rows
    backup.assert_not_awaited()


async def test_entity_restore_refuses_a_typed_edit_instead_of_rebasing_expected_image(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, registry, _, _, owner = _setup(native, monkeypatch)
    service = owner.NativeEntityRegistry(
        manager.hass, CallContext("actor"), authorize=manager.backend.authorize
    )
    saved = await service.read_snapshot("uuid-first")
    registry.rows["sensor.first"].future_core_field = 0
    result = await Engine(manager.backend).call_owned(
        "registry-restore",
        lambda: service.restore_snapshot("uuid-first", saved, saved),
        CallContext("actor"),
        writes=True,
    )
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    assert (
        registry.calls == []
        and type(registry.rows["sensor.first"].future_core_field) is int
    )


async def test_entity_restore_that_fails_after_one_phase_is_unknown_without_replay(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, registry, _, _, owner = _setup(native, monkeypatch)
    service = owner.NativeEntityRegistry(
        manager.hass, CallContext("actor"), authorize=manager.backend.authorize
    )
    saved = await service.read_snapshot("uuid-first")
    registry.rows["sensor.first"].name = "Changed"
    registry.rows["sensor.first"].options["added"] = {"value": True}
    expected = await service.read_snapshot("uuid-first")
    update = registry.async_update_entity_options

    def update_then_raise(*args: Any) -> Any:
        update(*args)
        raise ValueError("listener failed")

    monkeypatch.setattr(registry, "async_update_entity_options", update_then_raise)
    result = await Engine(manager.backend).call_owned(
        "registry-restore",
        lambda: service.restore_snapshot("uuid-first", saved, expected),
        CallContext("actor"),
        writes=True,
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert [call[0] for call in registry.calls] == ["update", "options"]


async def test_entity_restore_never_applies_later_options_to_a_listener_replacement(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, registry, _, _, owner = _setup(native, monkeypatch)
    service = owner.NativeEntityRegistry(
        manager.hass, CallContext("actor"), authorize=manager.backend.authorize
    )
    saved = await service.read_snapshot("uuid-first")
    registry.rows["sensor.first"].name = "Changed"
    registry.rows["sensor.first"].options["added"] = {"value": True}
    expected = await service.read_snapshot("uuid-first")
    update = registry.async_update_entity

    def replace_after_update(entity_id: str, **changes: Any) -> Any:
        written = update(entity_id, **changes)
        registry.rows[entity_id] = registry.new(entity_id, "replacement-uuid")
        registry.rows[entity_id].options = {"added": {"replacement": True}}
        return written

    monkeypatch.setattr(registry, "async_update_entity", replace_after_update)
    result = await Engine(manager.backend).call_owned(
        "registry-restore",
        lambda: service.restore_snapshot("uuid-first", saved, expected),
        CallContext("actor"),
        writes=True,
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert registry.rows["sensor.first"].options == {"added": {"replacement": True}}
    assert [call[0] for call in registry.calls] == ["update"]


async def test_entity_history_reports_effective_options_when_core_rematerializes_a_cleared_domain(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, manager, registry, _, _, owner = _setup(native, monkeypatch)
    expected = registry.rows["sensor.first"].extended_dict
    saved = {**expected, "options": {}}
    update = registry.async_update_entity_options

    def clear_then_suggest(entity_id: str, domain: str, value: Any) -> Any:
        written = update(entity_id, domain, value)
        if value is None:
            update(entity_id, domain, {"suggested_display_precision": 3})
        return written

    monkeypatch.setattr(registry, "async_update_entity_options", clear_then_suggest)
    service = owner.NativeEntityRegistry(
        manager.hass, CallContext("actor"), authorize=manager.backend.authorize
    )
    result = await engine.call_owned(
        "restore",
        lambda: service.restore_snapshot("uuid-first", saved, expected),
        CallContext("actor"),
        writes=True,
    )
    assert not result.is_error, result.data
    assert result.data["options"]["sensor"] == {"suggested_display_precision": 3}
    assert (
        result.data["options_succeeded"]["sensor"] == result.data["options"]["sensor"]
    )
    assert "Core" in result.data["warnings"][0]


async def test_bulk_removal_yields_to_queued_revocation_before_the_next_target(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, _, _, _ = _setup(native, monkeypatch)
    remove = registry.async_remove

    def remove_then_queue_disable(entity_id: str) -> None:
        remove(entity_id)
        asyncio.get_running_loop().call_soon(setattr, engine, "enabled_tools", set())

    monkeypatch.setattr(registry, "async_remove", remove_then_queue_disable)
    result = await engine.call(
        "ha_remove_entity",
        {"entity_id": ["sensor.first", "sensor.second"]},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "confirmed", result.data
    assert "sensor.first" not in registry.rows and "sensor.second" in registry.rows
    assert len(registry.calls) == 1
