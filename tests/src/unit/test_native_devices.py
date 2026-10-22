"""Device writes keep Core identities and own asynchronous removal callbacks."""

import asyncio
import copy
import importlib
import sys
from enum import StrEnum
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext

from .test_native_engine_backend import native as native
from .test_native_labels import _setup as label_setup


class _Device(SimpleNamespace):
    @property
    def dict_repr(self) -> dict[str, Any]:
        return {
            key: sorted(value) if isinstance(value, set) else copy.deepcopy(value)
            for key, value in vars(self).items()
        }


class _Child(_Device):
    pass


class _Composite(_Device):
    pass


class _Disabler(StrEnum):
    USER = "user"


class _Devices:
    def __init__(self) -> None:
        self.rows = {
            "main": self.new("main"),
            "child": self.new("child", _Child),
            "composite": self.new("composite", _Composite),
        }
        self.calls = []

    @staticmethod
    def new(identifier: str, kind: Any = _Device) -> _Device:
        return kind(
            id=identifier,
            name="From integration",
            name_by_user="Before",
            area_id=None,
            disabled_by=None,
            labels={"other"},
            config_entry_id="entry",
            created_at=1.0,
            modified_at=1.0,
            future_core_field=False,
        )

    def async_get(
        self,
        identifier: str,
        *,
        include_main_devices: bool = True,
        include_child_devices: bool = True,
        include_composite_devices: bool = True,
    ) -> Any:
        row = self.rows.get(identifier)
        if type(row) is _Device and not include_main_devices:
            return None
        if type(row) is _Child and not include_child_devices:
            return None
        if type(row) is _Composite and not include_composite_devices:
            return None
        return row

    def _update(self, kind: str, device_id: str, **data: Any) -> Any:
        previous = self.rows[device_id]
        written = type(previous)(**{**vars(previous), **copy.deepcopy(data)})
        self.rows[device_id] = written
        self.calls.append((kind, {"device_id": device_id, **copy.deepcopy(data)}))
        return written

    def async_update_device(self, device_id: str, **data: Any) -> Any:
        return self._update("main", device_id, **data)

    def async_update_child_device(self, device_id: str, **data: Any) -> Any:
        return self._update("child", device_id, **data)

    def async_remove_device(self, identifier: str) -> None:
        self.calls.append(("remove", identifier))
        del self.rows[identifier]


def _setup(
    native: Any, monkeypatch: pytest.MonkeyPatch, *, backup: Any = None
) -> tuple:
    _, manager, labels, areas, _, user, _ = label_setup(
        native, monkeypatch, backup=backup
    )
    devices = _Devices()
    device_module = SimpleNamespace(
        async_get=lambda hass: devices,
        ChildDeviceEntry=_Child,
        DeviceEntryDisabler=_Disabler,
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "device_registry", device_module
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.device_registry", device_module
    )
    entry = SimpleNamespace(
        entry_id="entry", domain="example", supports_remove_device=True
    )
    entries = {"entry": entry}
    manager.hass.config_entries = SimpleNamespace(async_get_entry=entries.get)
    component = SimpleNamespace(
        async_remove_config_entry_device=AsyncMock(return_value=True)
    )
    loader = SimpleNamespace(
        async_get_integration=AsyncMock(
            return_value=SimpleNamespace(
                async_get_component=AsyncMock(return_value=component),
            )
        )
    )
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader)
    monkeypatch.setattr(sys.modules["homeassistant"], "loader", loader, raising=False)
    schemas = []

    def validate(value: dict) -> dict:
        schemas.append(copy.deepcopy(value))
        if "name_by_user" in value and value["name_by_user"] is not None:
            value["name_by_user"] = value["name_by_user"].title()
        return value

    manager.hass.data["websocket_api"].update(
        {
            f"config/device_registry/{action}": (None, validate)
            for action in ("update", "remove")
        }
    )
    bindings = importlib.import_module(
        "custom_components.ha_mcp_tools.native_device_tools"
    )
    engine = Engine(manager.backend, bindings.build_device_tools(manager))
    return engine, manager, devices, component, entry, user, schemas, labels, areas


@pytest.mark.parametrize("device_id", ["main", "child"])
async def test_device_update_uses_its_core_owner_schema_and_full_serializer(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    device_id: str,
) -> None:
    backup = AsyncMock(return_value={"warnings": ["snapshot-note"]})
    engine, manager, devices, _, _, _, schemas, _, _ = _setup(
        native, monkeypatch, backup=backup
    )
    before = devices.rows[device_id].dict_repr
    result = await engine.call(
        "ha_set_device",
        {
            "device_id": device_id,
            "name": "new name",
            "area_id": "existing",
            "disabled_by": "user",
            "labels": '["existing"]',
        },
        CallContext("actor"),
    )
    assert not result.is_error, result.data
    assert devices.calls[0][0] == device_id
    assert devices.calls[0][1]["disabled_by"] is _Disabler.USER
    assert devices.calls[0][1]["labels"] == {"existing"}
    assert result.data["device_entry"]["name_by_user"] == "New Name"
    assert result.data["device_entry"]["future_core_field"] is False
    assert result.data["warnings"] == ["snapshot-note"]
    assert schemas[0]["type"] == "config/device_registry/update"
    assert (
        backup.await_count == 1 and backup.call_args.args[0]["current_config"] == before
    )
    manager.backend.execute.assert_not_awaited()


@pytest.mark.parametrize("tool", ["ha_set_device", "ha_remove_device"])
async def test_composite_device_cannot_be_mutated_or_captured(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    backup = AsyncMock()
    engine, _, devices, component, *_ = _setup(native, monkeypatch, backup=backup)
    result = await engine.call(
        tool,
        {
            "device_id": "composite",
            **({"name": "x"} if tool == "ha_set_device" else {}),
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert devices.calls == []
    backup.assert_not_awaited()
    component.async_remove_config_entry_device.assert_not_awaited()


@pytest.mark.parametrize("change", ["typed_edit", "revoke", "delete_area"])
async def test_device_rechecks_before_image_references_and_admission_after_capture(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    async def backup(payload: dict, actor: CallContext) -> dict:
        if change == "typed_edit":
            devices.rows["main"].future_core_field = 0
        elif change == "revoke":
            user.is_admin = False
        else:
            areas.entries.pop("existing")
        return {}

    engine, _, devices, _, _, user, _, _, areas = _setup(
        native, monkeypatch, backup=backup
    )
    result = await engine.call(
        "ha_set_device",
        {"device_id": "main", "area_id": "existing"},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started", result.data
    assert devices.calls == []
    if change == "typed_edit":
        assert result.data["error"]["code"] == "CONFIG_CONFLICT"
        assert type(devices.rows["main"].future_core_field) is int


async def test_device_update_failure_after_core_changes_is_unknown_once(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, devices, *_ = _setup(native, monkeypatch)
    original = devices.async_update_device

    def mutate_then_raise(**data: Any) -> Any:
        original(**data)
        raise ValueError("listener failed")

    monkeypatch.setattr(devices, "async_update_device", mutate_then_raise)
    result = await engine.call(
        "ha_set_device", {"device_id": "main", "name": "new"}, CallContext("actor")
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert devices.rows["main"].name_by_user == "New" and len(devices.calls) == 1


@pytest.mark.parametrize("integration_removes", [False, True])
async def test_device_remove_dispatches_integration_once_and_does_not_delete_twice(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    integration_removes: bool,
) -> None:
    backup = AsyncMock(return_value={})
    engine, _, devices, component, *_ = _setup(native, monkeypatch, backup=backup)

    async def remove(hass: Any, entry: Any, device: Any) -> bool:
        if integration_removes:
            devices.rows.pop(device.id)
        return True

    component.async_remove_config_entry_device.side_effect = remove
    result = await engine.call(
        "ha_remove_device", {"device_id": "main"}, CallContext("actor")
    )
    assert not result.is_error and result.data["config_entries_removed"] == 1, (
        result.data
    )
    assert "main" not in devices.rows
    assert devices.calls == ([] if integration_removes else [("remove", "main")])
    assert (
        component.async_remove_config_entry_device.await_count
        == backup.await_count
        == 1
    )


@pytest.mark.parametrize(
    "callback_effect",
    ["false", "false_after_mutation", "raise_after_mutation", "revoke", "recreate"],
)
async def test_device_callback_cannot_be_replayed_or_remove_a_replacement_after_await(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
    callback_effect: str,
) -> None:
    engine, _, devices, component, _, user, *_ = _setup(native, monkeypatch)

    async def callback(hass: Any, entry: Any, device: Any) -> bool:
        if callback_effect.endswith("mutation"):
            devices.rows.pop(device.id)
        if callback_effect == "revoke":
            user.is_admin = False
        if callback_effect == "recreate":
            devices.rows[device.id] = devices.new(device.id)
            devices.rows[device.id].created_at = 2.0
        if callback_effect == "raise_after_mutation":
            raise ValueError("failed after dispatch")
        return not callback_effect.startswith("false")

    component.async_remove_config_entry_device.side_effect = callback
    result = await engine.call(
        "ha_remove_device", {"device_id": "main"}, CallContext("actor")
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert (
        devices.calls == []
        and component.async_remove_config_entry_device.await_count == 1
    )
    if callback_effect.startswith("false"):
        assert result.data["integration_rejected"] is True
        assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    if callback_effect in {"recreate", "revoke", "false"}:
        assert "main" in devices.rows


async def test_device_callback_can_update_the_same_core_identity_before_removal(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, devices, component, *_ = _setup(native, monkeypatch)

    async def callback(hass: Any, entry: Any, device: Any) -> bool:
        devices.async_update_device(device.id, name="Fresh integration metadata")
        return True

    component.async_remove_config_entry_device.side_effect = callback
    result = await engine.call(
        "ha_remove_device", {"device_id": "main"}, CallContext("actor")
    )
    assert not result.is_error and "main" not in devices.rows, result.data
    assert [kind for kind, _ in devices.calls] == ["main", "remove"]
    assert component.async_remove_config_entry_device.await_count == 1


async def test_device_restore_keeps_cas_and_does_not_recapture(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = AsyncMock()
    _, manager, devices, *_ = _setup(native, monkeypatch, backup=backup)
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.devices"
    )
    service = owner.NativeDevices(
        manager.hass,
        CallContext("actor"),
        authorize=manager.backend.authorize,
        backup=backup,
    )
    before = await service.read_snapshot("main")
    saved = {**before, "name_by_user": "restored", "area_id": None, "labels": []}
    result = await Engine(manager.backend).call_owned(
        "device-restore",
        lambda: service.restore_snapshot("main", saved, before),
        CallContext("actor"),
        writes=True,
    )
    assert not result.is_error and devices.rows["main"].name_by_user == "Restored", (
        result.data
    )
    backup.assert_not_awaited()
    devices.rows["main"].future_core_field = 0
    refused = await Engine(manager.backend).call_owned(
        "device-restore",
        lambda: service.restore_snapshot("main", saved, before),
        CallContext("actor"),
        writes=True,
    )
    assert refused.is_error and refused.data["error"]["code"] == "CONFIG_CONFLICT"
    assert len(devices.calls) == 1


@pytest.mark.parametrize("change", ["revoke", "replace", "feature"])
async def test_device_owned_callback_rechecks_admission_after_task_scheduling(
    native: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    engine, _, devices, component, entry, user, *_ = _setup(native, monkeypatch)
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.devices"
    )
    owned = owner.await_owned_io

    async def changed(awaitable: Any) -> Any:
        if change == "revoke":
            user.is_admin = False
        elif change == "replace":
            devices.rows["main"] = devices.new("main")
            devices.rows["main"].created_at = 5.0
        else:
            entry.supports_remove_device = False
        return await owned(awaitable)

    monkeypatch.setattr(owner, "await_owned_io", changed)
    result = await engine.call(
        "ha_remove_device", {"device_id": "main"}, CallContext("actor")
    )
    assert result.is_error and result.status == "not_started", result.data
    component.async_remove_config_entry_device.assert_not_awaited()
    assert not devices.calls and "main" in devices.rows


async def test_device_cancellation_keeps_registry_lock_until_integration_callback_finishes(
    native: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, devices, component, *_ = _setup(native, monkeypatch)
    owner = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.devices"
    )
    service = owner.NativeDevices(
        manager.hass, CallContext("actor"), authorize=manager.backend.authorize
    )
    entered, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def remove(hass: Any, entry: Any, device: Any) -> bool:
        entered.set()
        await release.wait()
        return True

    component.async_remove_config_entry_device.side_effect = remove
    task = asyncio.create_task(service.remove("main"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()

    async def second_writer() -> None:
        async with owner.registry_update_lock("device", "main"):
            second_entered.set()

    second = asyncio.create_task(second_writer())
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await second
    assert second_entered.is_set() and "main" in devices.rows
    assert (
        component.async_remove_config_entry_device.await_count == 1
        and devices.calls == []
    )
