"""Canonical device writes use the actual registry and integration callbacks."""

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.device_values import device_update_result, device_update_values
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.registry_references import (
    _raise_if_unknown_area,
    _raise_if_unknown_labels,
)
from ha_mcp.runtime.snapshot_payloads import InvalidBackupSnapshotError
from ha_mcp.runtime.validation import validate_identifier_not_empty
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .command_validation import _validated_payload
from .device_updates import commit_device_update, prepare_device_update
from .registry_access import (
    Authorize,
    Capture,
    admit_registry_write,
    authorize_actor,
    capture_registry_before,
    registry_failure,
    registry_json,
)
from .registry_failures import registry_outcome_unknown, registry_target_missing

_CURRENT = object()


@dataclass(slots=True)
class NativeDevices:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    backup: Capture | None = field(default=None, kw_only=True, repr=False)

    def _owner(self) -> Any:
        from homeassistant.helpers import device_registry as dr

        return dr.async_get(self.hass)

    @staticmethod
    def _target(registry: Any, device_id: str) -> Any:
        if (
            registry.async_get(
                device_id, include_main_devices=False, include_child_devices=False
            )
            is not None
        ):
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "Cannot change a composite device; select an underlying device.",
                status="not_started",
            )
        device = registry.async_get(device_id, include_composite_devices=False)
        if device is None:
            registry_target_missing("device", device_id)
        return device

    def _check_references(self, payload: dict[str, Any]) -> None:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import label_registry as lr

        if payload.get("area_id"):
            _raise_if_unknown_area(
                [
                    {"area_id": row.id}
                    for row in ar.async_get(self.hass).async_list_areas()
                ],
                payload["area_id"],
            )
        if payload.get("labels"):
            _raise_if_unknown_labels(
                [
                    {"label_id": row.label_id}
                    for row in lr.async_get(self.hass).async_list_labels()
                ],
                payload["labels"],
            )

    def _check_current(
        self, registry: Any, device_id: str, before: dict[str, Any]
    ) -> Any:
        current_owner = self._owner()
        current = self._target(current_owner, device_id)
        if current_owner is not registry or not json_values_equal(
            registry_json(current.dict_repr), before
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The device changed after its before-image was read; no further mutation was sent.",
                status="not_started",
            )
        return current

    async def set(
        self,
        device_id: str,
        name: str | None = None,
        area_id: str | None = None,
        disabled_by: str | None = None,
        labels: str | list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            validate_identifier_not_empty(device_id, "device_id")
            payload, updates = device_update_values(
                device_id, name, area_id, disabled_by, labels
            )
            row, warnings = await self._update(payload)
            return device_update_result(device_id, row, updates, warnings)
        except Exception as error:
            raise registry_failure(error, {"device_id": device_id}) from error

    async def _update(
        self,
        payload: dict[str, Any],
        *,
        expected: Any = _CURRENT,
        capture: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:

        device_id = payload["device_id"]
        async with registry_update_lock("device", device_id):
            registry = self._owner()
            device = self._target(registry, device_id)
            before = registry_json(device.dict_repr)
            if expected is not _CURRENT and not json_values_equal(before, expected):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The device changed after its restore safety capture.",
                    status="not_started",
                )
            validated = _validated_payload(
                self.hass, "config/device_registry/update", payload
            )
            self._check_references(validated)
            warnings = (
                await capture_registry_before(
                    self.backup,
                    self.actor,
                    "device",
                    device_id,
                    before,
                    "ha_set_device",
                )
                if capture
                else []
            )
            await admit_registry_write(self.actor, self.authorize)
            device = self._check_current(registry, device_id, before)
            self._check_references(validated)
            validated = prepare_device_update(self.hass, validated)
            check_mutation_admission(self.actor)
            try:
                row = commit_device_update(registry, device, validated)
                return row, warnings
            except Exception as error:  # noqa: BLE001 - owner/listeners/serializer may fail after mutation
                return registry_outcome_unknown("device", "update", error)

    async def remove(self, device_id: str) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            validate_identifier_not_empty(device_id, "device_id")
            async with registry_update_lock("device", device_id):
                return await self._remove(device_id)
        except Exception as error:
            raise registry_failure(error, {"device_id": device_id}) from error

    async def _remove(self, device_id: str) -> dict[str, Any]:
        from homeassistant import loader

        _validated_payload(
            self.hass, "config/device_registry/remove", {"device_id": device_id}
        )
        registry = self._owner()
        device = self._target(registry, device_id)
        before = registry_json(device.dict_repr)
        entry = self.hass.config_entries.async_get_entry(device.config_entry_id)
        if entry is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Unknown config entry", status="not_started"
            )
        if not entry.supports_remove_device:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "Config entry does not support device removal",
                status="not_started",
            )
        try:
            integration = await loader.async_get_integration(self.hass, entry.domain)
            component = await integration.async_get_component()
        except (ImportError, loader.IntegrationNotFound) as error:
            raise ToolFailure(
                "SERVICE_CALL_FAILED", "Integration not found", status="not_started"
            ) from error
        await admit_registry_write(self.actor, self.authorize)
        self._check_current(registry, device_id, before)
        if self.hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The owning config entry changed during preparation.",
                status="not_started",
            )
        warnings = await capture_registry_before(
            self.backup, self.actor, "device", device_id, before, "ha_remove_device"
        )
        await self._dispatch_remove(component, registry, entry, device, before)
        result = {
            "success": True,
            "device_id": device_id,
            "device_name": before.get("name_by_user") or before.get("name"),
            "config_entries_removed": 1,
            "message": "Successfully removed device from 1 config entry/entries",
        }
        if warnings:
            result["warnings"] = warnings
        return result

    def _remove_target(
        self, registry: Any, entry: Any, device: Any, before: dict[str, Any]
    ) -> Any:
        device = self._check_current(registry, device.id, before)
        if self.hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The owning config entry changed before dispatch.",
                status="not_started",
            )
        if not entry.supports_remove_device:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "Config entry no longer supports device removal",
                status="not_started",
            )
        return device

    async def _invoke_remove(
        self,
        component: Any,
        registry: Any,
        entry: Any,
        device: Any,
        before: dict[str, Any],
    ) -> bool:
        # This runs inside the owned task. Its scheduling must precede the final
        # actor/identity checks, otherwise revocation can race callback startup.
        await admit_registry_write(self.actor, self.authorize)
        device = self._remove_target(registry, entry, device, before)
        check_mutation_admission(self.actor)
        try:
            accepted: bool = await component.async_remove_config_entry_device(
                self.hass, entry, device
            )
            return accepted
        except Exception as error:  # noqa: BLE001 - the integration callback has started
            return registry_outcome_unknown("device", "remove", error)

    async def _dispatch_remove(
        self,
        component: Any,
        registry: Any,
        entry: Any,
        device: Any,
        before: dict[str, Any],
    ) -> None:
        device_id = device.id
        anchor = self._removal_anchor(device)
        accepted = await await_owned_io(
            self._invoke_remove(component, registry, entry, device, before)
        )
        try:
            if not accepted:
                failure = ToolFailure(
                    "OUTCOME_UNKNOWN",
                    "The integration rejected removal after its callback was dispatched; inspect the device before retrying.",
                    status="outcome_unknown",
                )
                failure.data["integration_rejected"] = True
                raise failure
            # The integration callback can remove the device itself. Never remove
            # an ID recreated during that await, nor dispatch a second mutation
            # after revocation. Admission failures here cannot undo the callback.
            await admit_registry_write(self.actor, self.authorize)
            current_owner = self._owner()
            current = current_owner.async_get(
                device_id, include_composite_devices=False
            )
            if (
                current_owner is not registry
                or self.hass.config_entries.async_get_entry(entry.entry_id) is not entry
            ):
                raise RuntimeError(
                    "Device or config-entry owner changed during integration callback"
                )
            if current is not None:
                current_anchor = self._removal_anchor(current)
                if current_anchor[0] is not anchor[0] or not json_values_equal(
                    current_anchor[1], anchor[1]
                ):
                    raise RuntimeError(
                        "Device identity changed during integration callback"
                    )
                check_mutation_admission(self.actor)
                registry.async_remove_device(device_id)
            if registry.async_get(device_id) is not None:
                raise RuntimeError("Core did not confirm the device removal")
        except ToolFailure as error:
            if error.status == "outcome_unknown":
                raise
            registry_outcome_unknown("device", "remove", error)
        except Exception as error:  # noqa: BLE001 - integration and registry may have already changed state
            registry_outcome_unknown("device", "remove", error)

    @staticmethod
    def _removal_anchor(device: Any) -> tuple[type, dict[str, Any]]:
        row = registry_json(device.dict_repr)
        if any(row.get(key) is None for key in ("id", "created_at", "config_entry_id")):
            raise UnsupportedOperation("devices.remove.identity")
        return type(device), {
            key: row.get(key)
            for key in ("id", "created_at", "config_entry_id", "parent_device_id")
        }

    async def read_snapshot(self, device_id: str) -> dict[str, Any] | None:
        await authorize_actor(self.actor, self.authorize)
        registry = self._owner()
        if registry.async_get(device_id) is None:
            return None
        return registry_json(self._target(registry, device_id).dict_repr)

    async def restore_snapshot(
        self,
        device_id: str,
        config: dict[str, Any],
        expected: Any,
    ) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.authorize)
        if config.get("id") != device_id:
            raise InvalidBackupSnapshotError(
                "Device snapshot does not match its identity"
            )
        row, _ = await self._update(
            {
                "device_id": device_id,
                "name_by_user": config.get("name_by_user"),
                "area_id": config.get("area_id"),
                "disabled_by": config.get("disabled_by"),
                "labels": config.get("labels", []),
            },
            expected=expected,
            capture=False,
        )
        return row
