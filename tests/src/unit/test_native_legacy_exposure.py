"""Preferences without a registry entry preserve absence and Core notifications."""

import copy
import importlib
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_entity_registry import _setup as entity_setup


@dataclass(frozen=True)
class _ExposedEntity:
    assistants: dict[str, dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {"assistants": self.assistants}


class _Owner:
    def __init__(self) -> None:
        self.entities: dict[str, _ExposedEntity] = {}
        self._listeners: dict[str, list[Any]] = {}
        self.saves = 0
        self.calls = []
        self.fail_save = False

    def _async_schedule_save(self) -> None:
        self.saves += 1
        if self.fail_save:
            raise ValueError("Core store schedule failed")

    def expose(self, assistant: str, entity_id: str, value: bool) -> None:
        before = self.entities.get(entity_id)
        values = copy.deepcopy(before.assistants if before else {})
        values[assistant] = {**values.get(assistant, {}), "should_expose": value}
        self.entities[entity_id] = _ExposedEntity(values)
        self.calls.append((assistant, entity_id, value))
        self._async_schedule_save()
        for listener in self._listeners.get(assistant, []):
            listener()


def _setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    _, manager, registry, _, user, _ = entity_setup(native, monkeypatch)
    owner = _Owner()
    module = SimpleNamespace(
        ExposedEntity=_ExposedEntity,
        ExposedEntities=_Owner,
        DATA_EXPOSED_ENTITIES="exposed",
        async_expose_entity=lambda hass, assistant, entity_id, value: hass.data[
            "exposed"
        ].expose(assistant, entity_id, value),
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.homeassistant.exposed_entities", module
    )
    parent = sys.modules.get("homeassistant.components.homeassistant")
    if parent is None:
        parent = SimpleNamespace(exposed_entities=module)
        monkeypatch.setitem(
            sys.modules, "homeassistant.components.homeassistant", parent
        )
    else:
        monkeypatch.setattr(parent, "exposed_entities", module, raising=False)
    manager.hass.data["exposed"] = owner
    manager.hass.data["websocket_api"]["homeassistant/expose_entity"] = (None, dict)
    operations = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.legacy_exposure"
    )
    service = operations.NativeLegacyExposure(
        manager.hass,
        CallContext("actor"),
        authorize=manager.backend.authorize,
        backup=backup or AsyncMock(return_value={}),
    )
    return service, owner, manager, registry, user


async def _call(manager: Any, method: Any) -> Any:
    return await Engine(manager.backend).call_owned(
        "legacy-exposure", method, CallContext("actor"), writes=True
    )


async def test_legacy_exposure_absence_is_captured_and_restored_as_absence(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = AsyncMock(return_value={})
    service, owner, manager, _, _ = _setup(native, monkeypatch, backup=backup)
    notifications = []
    owner._listeners = {
        assistant: [lambda assistant=assistant: notifications.append(assistant)]
        for assistant in ("conversation", "cloud.alexa")
    }
    saved = await service.read_snapshot("sensor.legacy")
    result = await _call(
        manager,
        lambda: service.set(
            "sensor.legacy", {"conversation": True, "cloud.alexa": False}
        ),
    )
    assert not result.is_error and owner.saves == 2, result.data
    assert backup.call_args.args[0] == {
        "domain": "entity_exposure",
        "source_tool": "ha_set_entity",
        "backup_id": "sensor.legacy",
        "current_config": saved,
    }
    assert saved == {"entity_id": "sensor.legacy", "preferences": None}
    expected = await service.read_snapshot("sensor.legacy")
    restored = await _call(
        manager, lambda: service.restore_snapshot("sensor.legacy", saved, expected)
    )
    assert not restored.is_error and "sensor.legacy" not in owner.entities, (
        restored.data
    )
    assert owner.saves == 3 and sorted(notifications) == [
        "cloud.alexa",
        "cloud.alexa",
        "conversation",
        "conversation",
    ]
    assert backup.await_count == 1
    manager.backend.execute.assert_not_awaited()


async def test_legacy_exposure_restore_preserves_explicit_false_and_unrelated_future_options(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, owner, manager, _, _ = _setup(native, monkeypatch)
    saved = {
        "entity_id": "sensor.legacy",
        "preferences": {
            "assistants": {
                "conversation": {"should_expose": False, "future_option": {"value": 7}}
            }
        },
    }
    owner.entities["other"] = _ExposedEntity({"conversation": {"should_expose": True}})
    before = await service.read_snapshot("sensor.legacy")
    result = await _call(
        manager, lambda: service.restore_snapshot("sensor.legacy", saved, before)
    )
    assert not result.is_error and result.data == saved, result.data
    assert (
        owner.entities["sensor.legacy"].assistants["conversation"]["should_expose"]
        is False
    )
    assert owner.entities["other"].assistants == {
        "conversation": {"should_expose": True}
    }


@pytest.mark.parametrize("change", ["typed_edit", "register", "revoke", "owner"])
async def test_legacy_exposure_refuses_changed_image_or_owner_after_capture(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        if change == "typed_edit":
            owner.entities["sensor.legacy"].assistants["conversation"][
                "should_expose"
            ] = 0
        elif change == "register":
            registry.rows["sensor.legacy"] = registry.new(
                "sensor.legacy", "new-registration"
            )
        elif change == "revoke":
            user.is_admin = False
        else:
            manager.hass.data["exposed"] = _Owner()
        return {}

    service, owner, manager, registry, user = _setup(native, monkeypatch, backup=backup)
    owner.entities["sensor.legacy"] = _ExposedEntity(
        {"conversation": {"should_expose": False}}
    )
    result = await _call(
        manager, lambda: service.set("sensor.legacy", {"conversation": True})
    )
    assert result.is_error and result.status == "not_started", result.data
    assert owner.calls == [] and owner.saves == 0


async def test_legacy_exposure_save_failure_after_core_mapping_update_is_unknown_once(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, owner, manager, _, _ = _setup(native, monkeypatch)
    owner.fail_save = True
    result = await _call(
        manager, lambda: service.set("sensor.legacy", {"conversation": True})
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert (
        owner.entities["sensor.legacy"].assistants["conversation"]["should_expose"]
        is True
    )
    assert owner.calls == [("conversation", "sensor.legacy", True)] and owner.saves == 1


async def test_legacy_exposure_listener_registration_stops_later_phases_and_keeps_known_progress(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, owner, manager, registry, _ = _setup(native, monkeypatch)

    def register() -> None:
        registry.rows["sensor.legacy"] = registry.new(
            "sensor.legacy", "new-registry-entry"
        )

    owner._listeners["cloud.alexa"] = [register]
    result = await _call(
        manager,
        lambda: service.set(
            "sensor.legacy",
            {
                "conversation": True,
                "cloud.alexa": False,
                "cloud.google_assistant": False,
            },
        ),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert result.data["exposure_succeeded"] == {"conversation": True}
    assert len(owner.calls) == 2 and registry.calls == []


async def test_legacy_exposure_restore_listener_error_after_delete_has_no_fake_rollback(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, owner, manager, _, _ = _setup(native, monkeypatch)
    owner.entities["sensor.legacy"] = _ExposedEntity(
        {"conversation": {"should_expose": True}}
    )

    def fail() -> None:
        raise ValueError("Core listener failed")

    owner._listeners["conversation"] = [fail]
    before = await service.read_snapshot("sensor.legacy")
    result = await _call(
        manager,
        lambda: service.restore_snapshot(
            "sensor.legacy", {"entity_id": "sensor.legacy", "preferences": None}, before
        ),
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert "sensor.legacy" not in owner.entities and owner.saves == 1


async def test_private_exposure_adapter_refuses_a_subclass_with_another_commit_protocol(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, manager, _, _ = _setup(native, monkeypatch)

    class DifferentProtocol(_Owner):
        def expose(self, assistant: str, entity_id: str, value: bool) -> None:
            super().expose(assistant, entity_id, value)
            self.entities["unrelated"] = _ExposedEntity(
                {assistant: {"should_expose": value}}
            )

    replacement = DifferentProtocol()
    manager.hass.data["exposed"] = replacement
    result = await _call(
        manager, lambda: service.set("sensor.legacy", {"conversation": True})
    )
    assert (
        result.is_error and result.data["error"]["code"] == "UNSUPPORTED_OPERATION"
    ), result.data
    assert replacement.calls == [] and replacement.entities == {}
