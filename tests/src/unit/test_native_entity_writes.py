"""Compound entity changes capture owners once and keep explicit partial outcomes."""

import asyncio
import copy
import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_devices import august_registry
from .test_native_engine_backend import native as native
from .test_native_legacy_exposure import _setup as exposure_setup


def _setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    _, exposure_owner, manager, registry, user = exposure_setup(native, monkeypatch)
    manager.snapshots.registry_backup = backup or AsyncMock(return_value={})
    validator = manager.hass.data["websocket_api"]["config/entity_registry/update"]
    manager.hass.data["websocket_api"]["config/entity_registry/get"] = validator
    module = sys.modules["homeassistant.components.homeassistant.exposed_entities"]

    def expose(hass: Any, assistant: str, entity_id: str, value: bool) -> None:
        entry = registry.async_get(entity_id)
        if entry is None:
            exposure_owner.expose(assistant, entity_id, value)
            return
        options = {**entry.options.get(assistant, {}), "should_expose": value}
        registry.async_update_entity_options(entity_id, assistant, options)
        for listener in exposure_owner._listeners.get(assistant, []):
            listener()

    monkeypatch.setattr(module, "async_expose_entity", expose)
    bindings = importlib.import_module(
        "custom_components.ha_mcp_tools.native_entity_tools"
    )
    engine = Engine(manager.backend, [bindings.entity_write_definition(manager)])
    devices = sys.modules["homeassistant.helpers.device_registry"].async_get(
        manager.hass
    )
    return engine, manager, registry, devices, exposure_owner, user, module


@pytest.mark.parametrize("core_august", [False, True])
async def test_compound_entity_write_captures_both_real_owners_before_any_core_callback(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    core_august: bool,
) -> None:
    captures = []

    async def backup(payload: dict, actor: CallContext) -> dict:
        assert registry.calls == [] and devices.calls == []
        captures.append(copy.deepcopy(payload))
        return {"warnings": [f"{payload['domain']} captured"]}

    engine, manager, registry, devices, _, _, _ = _setup(
        native, monkeypatch, backup=backup
    )
    if core_august:
        august_registry(devices, monkeypatch)
    registry.rows["sensor.first"].device_id = "main"
    registry.rows["sensor.first"].categories = {"automation": "old"}
    before = registry.rows["sensor.first"].extended_dict
    device_before = devices.rows["main"].dict_repr
    result = await engine.call(
        "ha_set_entity",
        {
            "entity_id": "sensor.first",
            "new_entity_id": "sensor.renamed",
            "name": "Custom",
            "area_id": "existing",
            "hidden": True,
            "device_class": "energy",
            "aliases": [" New alias "],
            "categories": {"automation": None},
            "labels": ["existing"],
            "label_operation": "add",
            "options": {
                "sensor": {"precision": 2},
                "future_domain": {"arbitrary": [1, 2]},
            },
            "new_device_name": "renamed device",
            "expose_to": {"conversation": True, "cloud.alexa": False},
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert [(row["domain"], row["backup_id"]) for row in captures] == [
        ("entity_registry", "uuid-first"),
        ("device", "main"),
    ]
    assert (
        captures[0]["current_config"] == before
        and captures[1]["current_config"] == device_before
    )
    entry = registry.rows["sensor.renamed"]
    assert "sensor.first" not in registry.rows and entry.categories == {}
    assert entry.aliases == ["<core-computed-name>", "New alias"]
    assert entry.labels == {"existing", "other"}
    assert entry.options["future_domain"] == {"arbitrary": [1, 2]}
    assert (
        devices.rows["main"].name_by_user == "Renamed Device"
        and len(devices.calls) == 1
    )
    assert result.data["entity_entry"]["future_core_field"] is False
    assert result.data["exposure"] == {"conversation": True, "cloud.alexa": False}
    assert result.data["old_entity_id"] == "sensor.first"
    manager.backend.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "target,when",
    [
        ("entity", "entity_registry"),
        ("device", "entity_registry"),
        ("device", "device"),
    ],
)
async def test_compound_write_never_rebases_either_owner_after_capture(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    when: str,
) -> None:
    captures = []

    async def backup(payload: dict, actor: CallContext) -> dict:
        captures.append(payload["domain"])
        if payload["domain"] == when:
            row = (
                registry.rows["sensor.first"]
                if target == "entity"
                else devices.rows["main"]
            )
            row.future_core_field = 0
        return {}

    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch, backup=backup)
    registry.rows["sensor.first"].device_id = "main"
    result = await engine.call(
        "ha_set_entity",
        {
            "entity_id": "sensor.first",
            "name": "After",
            "new_device_name": "After device",
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert result.data["error"]["code"] == "CONFIG_CONFLICT"
    assert registry.calls == devices.calls == []
    assert captures == (
        ["entity_registry", "device"] if when == "device" else ["entity_registry"]
    )


async def test_compound_write_rechecks_role_before_capturing_the_second_owner(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = []

    async def backup(payload: dict, actor: CallContext) -> dict:
        captures.append(payload["domain"])
        user.is_admin = False
        return {}

    engine, _, registry, devices, _, user, _ = _setup(
        native, monkeypatch, backup=backup
    )
    registry.rows["sensor.first"].device_id = "main"
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "name": "After", "new_device_name": "After"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert captures == ["entity_registry"] and registry.calls == devices.calls == []


async def test_compound_write_revalidates_references_deleted_during_snapshot_io(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        labels.rows.pop("existing")
        return {}

    engine, manager, registry, _, _, _, _ = _setup(native, monkeypatch, backup=backup)
    labels = sys.modules["homeassistant.helpers.label_registry"].async_get(manager.hass)
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "labels": ["existing"]},
        CallContext("actor"),
    )
    assert (
        result.is_error
        and result.data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    ), result.data
    assert registry.calls == []


async def test_compound_options_failure_keeps_the_first_confirmed_phase_and_never_renames_device(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch)
    registry.rows["sensor.first"].device_id = "main"
    update = registry.async_update_entity_options

    def update_then_raise(*args: Any) -> Any:
        update(*args)
        raise ValueError("listener failed after options commit")

    monkeypatch.setattr(registry, "async_update_entity_options", update_then_raise)
    result = await engine.call(
        "ha_set_entity",
        {
            "entity_id": "sensor.first",
            "name": "After",
            "options": {"sensor": {"precision": 9}},
            "new_device_name": "After",
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert (
        result.data["confirmed_phases"] == ["registry"]
        and result.data["partial"] is True
    )
    assert registry.rows["sensor.first"].options["sensor"] == {"precision": 9}
    assert [call[0] for call in registry.calls] == [
        "update",
        "options",
    ] and devices.calls == []


async def test_entity_listener_device_change_is_not_overwritten_by_a_later_compound_phase(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch)
    registry.rows["sensor.first"].device_id = "main"
    update = registry.async_update_entity

    def update_then_change_device(*args: Any, **kwargs: Any) -> Any:
        written = update(*args, **kwargs)
        devices.rows["main"].name_by_user = "Changed by listener"
        return written

    monkeypatch.setattr(registry, "async_update_entity", update_then_change_device)
    result = await engine.call(
        "ha_set_entity",
        {
            "entity_id": "sensor.first",
            "name": "After",
            "new_device_name": "Requested device",
        },
        CallContext("actor"),
    )
    assert (
        result.is_error and result.status == "confirmed" and result.data["partial"]
    ), result.data
    assert registry.rows["sensor.first"].name == "After"
    assert (
        devices.rows["main"].name_by_user == "Changed by listener"
        and devices.calls == []
    )


async def test_compound_phase_never_updates_a_listener_replacement_using_the_old_entity_name(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch)
    registry.rows["sensor.first"].device_id = "main"
    update = registry.async_update_entity

    def update_then_replace(entity_id: str, **kwargs: Any) -> Any:
        written = update(entity_id, **kwargs)
        registry.rows[entity_id] = registry.new(entity_id, "replacement")
        return written

    monkeypatch.setattr(registry, "async_update_entity", update_then_replace)
    result = await engine.call(
        "ha_set_entity",
        {
            "entity_id": "sensor.first",
            "name": "After",
            "options": {"sensor": {"precision": 9}},
            "new_device_name": "Requested",
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert registry.rows["sensor.first"].id == "replacement"
    assert registry.rows["sensor.first"].options == {"sensor": {"precision": 1}}
    assert len(registry.calls) == 1 and devices.calls == []


async def test_registry_and_legacy_exposure_entities_share_bulk_results_and_keep_distinct_history(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = AsyncMock(return_value={})
    engine, manager, registry, _, exposure, _, _ = _setup(
        native, monkeypatch, backup=backup
    )
    result = await engine.call(
        "ha_set_entity",
        {
            "entity_id": ["sensor.first", "sensor.legacy"],
            "expose_to": {"conversation": True},
        },
        CallContext("actor"),
    )
    assert not result.is_error and result.data["succeeded_count"] == 2, result.data
    assert (
        registry.rows["sensor.first"].options["conversation"]["should_expose"] is True
    )
    assert (
        exposure.entities["sensor.legacy"].assistants["conversation"]["should_expose"]
        is True
    )
    assert [
        (call.args[0]["domain"], call.args[0]["backup_id"])
        for call in backup.call_args_list
    ] == [("entity_registry", "uuid-first"), ("entity_exposure", "sensor.legacy")]
    assert result.data["succeeded"][1]["entity_entry"] is None
    manager.backend.execute.assert_not_awaited()


@pytest.mark.parametrize("blank", ["", " "])
async def test_blank_device_name_warns_only_when_other_fields_are_committed(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    blank: str,
) -> None:
    backup = AsyncMock(return_value={})
    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch, backup=backup)
    registry.rows["sensor.first"].device_id = "main"
    refused = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "new_device_name": blank},
        CallContext("actor"),
    )
    assert refused.is_error and backup.await_count == 0 and registry.calls == []
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "name": "After", "new_device_name": blank},
        CallContext("actor"),
    )
    assert not result.is_error and "ha_set_device" in result.data["warnings"][0], (
        result.data
    )
    assert len(registry.calls) == backup.await_count == 1 and devices.calls == []


@pytest.mark.parametrize("use_device_name", [True, False])
async def test_entity_use_device_name_keeps_empty_name_distinct_from_default_name(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    use_device_name: bool,
) -> None:
    engine, _, registry, _, _, _, _ = _setup(native, monkeypatch)
    registry.rows["sensor.first"].device_id = "main"
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "use_device_name": use_device_name},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert registry.rows["sensor.first"].name == ("" if use_device_name else None)


async def test_bulk_refusal_after_confirmed_exposure_cannot_claim_no_write_started(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, _, _, _, module = _setup(native, monkeypatch)
    expose = module.async_expose_entity

    def expose_then_disable(*args: Any) -> None:
        expose(*args)
        engine.enabled_tools = set()

    monkeypatch.setattr(module, "async_expose_entity", expose_then_disable)
    result = await engine.call(
        "ha_set_entity",
        {
            "entity_id": ["sensor.first", "sensor.second"],
            "expose_to": {"conversation": True, "cloud.alexa": False},
        },
        CallContext("actor"),
    )
    assert (
        result.is_error and result.status == "confirmed" and result.data["partial"]
    ), result.data
    assert (
        registry.rows["sensor.first"].options["conversation"]["should_expose"] is True
    )
    assert "conversation" not in registry.rows["sensor.second"].options
    assert result.data["failed"][0]["details"]["exposure_succeeded"] == {
        "conversation": True
    }


async def test_device_disabled_during_backup_blocks_entity_enable_before_dispatch(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        devices.rows["main"].disabled = True
        return {}

    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch, backup=backup)
    registry.rows["sensor.first"].device_id = "main"
    registry.rows["sensor.first"].disabled_by = "user"
    devices.rows["main"].disabled = False
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "enabled": True},
        CallContext("actor"),
    )
    assert (
        result.is_error and result.status == "not_started" and registry.calls == []
    ), result.data
    assert registry.rows["sensor.first"].disabled_by == "user"


@pytest.mark.parametrize(
    "proposed,replaced",
    [
        ({"precision": 9}, {"precision": 99}),
        ({"future_flag": False}, {"future_flag": 0}),
    ],
)
async def test_entity_options_listener_cannot_make_unstored_requested_values_look_confirmed(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    proposed: dict,
    replaced: dict,
) -> None:
    engine, _, registry, _, _, _, _ = _setup(native, monkeypatch)
    update = registry.async_update_entity_options

    def update_then_replace_options(entity_id: str, domain: str, values: Any) -> Any:
        written = update(entity_id, domain, values)
        replacement = copy.deepcopy(written)
        replacement.options[domain] = replaced
        registry.rows[entity_id] = replacement
        return written

    monkeypatch.setattr(
        registry, "async_update_entity_options", update_then_replace_options
    )
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "options": {"sensor": proposed}},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert result.data["options_succeeded"] == {}
    assert registry.rows["sensor.first"].options["sensor"] == replaced
    assert len(registry.calls) == 1


async def test_entity_options_accept_core_derived_precision_and_return_the_actual_domain(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, _, _, _, _ = _setup(native, monkeypatch)
    update = registry.async_update_entity_options

    def update_with_core_suggestion(entity_id: str, domain: str, values: Any) -> Any:
        written = update(entity_id, domain, values)
        update(entity_id, domain, {**values, "suggested_display_precision": 3})
        return written

    monkeypatch.setattr(
        registry, "async_update_entity_options", update_with_core_suggestion
    )
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "options": {"sensor": {"display_precision": 2}}},
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    actual = {"display_precision": 2, "suggested_display_precision": 3}
    assert result.data["entity_entry"]["options"]["sensor"] == actual
    assert result.data["options_succeeded"]["sensor"] == actual
    assert any("Core" in warning for warning in result.data["warnings"])
    assert len(registry.calls) == 2


async def test_entity_listener_association_change_cannot_rename_the_previous_device(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, devices, _, _, _ = _setup(native, monkeypatch)
    registry.rows["sensor.first"].device_id = "main"
    update = registry.async_update_entity

    def update_then_reassociate(entity_id: str, **changes: Any) -> Any:
        written = update(entity_id, **changes)
        replacement = copy.deepcopy(written)
        replacement.device_id = "child"
        registry.rows[entity_id] = replacement
        return written

    monkeypatch.setattr(registry, "async_update_entity", update_then_reassociate)
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": "sensor.first", "name": "After", "new_device_name": "Requested"},
        CallContext("actor"),
    )
    assert result.is_error and devices.calls == [], result.data
    assert devices.rows["main"].name_by_user == "Before"


async def test_bulk_entity_writes_yield_to_queued_policy_changes_between_targets(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, registry, _, _, _, _ = _setup(native, monkeypatch)
    update = registry.async_update_entity

    def update_then_queue_disable(*args: Any, **changes: Any) -> Any:
        written = update(*args, **changes)
        asyncio.get_running_loop().call_soon(setattr, engine, "enabled_tools", set())
        return written

    monkeypatch.setattr(registry, "async_update_entity", update_then_queue_disable)
    result = await engine.call(
        "ha_set_entity",
        {"entity_id": ["sensor.first", "sensor.second"], "labels": ["existing"]},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "confirmed", result.data
    assert registry.rows["sensor.first"].labels == {"existing"}
    assert (
        registry.rows["sensor.second"].labels == {"other"} and len(registry.calls) == 1
    )
