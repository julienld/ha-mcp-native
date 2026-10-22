"""Entity registry removal and history use direct, identity-owned Core callbacks."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.entity_removal_values import (
    removal_targets,
    storage_note_unread,
    stored_helper_removal_note,
)
from ha_mcp.runtime.entity_snapshot_values import (
    entity_registry_identity,
    entity_restore_payloads,
)
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.registry_references import (
    _raise_if_unknown_area,
    _raise_if_unknown_category,
    _raise_if_unknown_labels,
)
from ha_mcp.runtime.snapshot_payloads import InvalidBackupSnapshotError
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..helper_collections import SIMPLE_HELPER_TYPES, collection_owner
from .command_validation import _validated_payload
from .registry_access import (
    Authorize,
    Capture,
    admit_registry_write,
    authorize_actor,
    capture_registry_before,
    registry_failure,
    registry_json,
)
from .registry_failures import registry_outcome_unknown


@dataclass(slots=True)
class NativeEntityRegistry:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    backup: Capture | None = field(default=None, kw_only=True, repr=False)

    def _owner(self) -> Any:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(self.hass)

    def _stored_note(self, row: dict[str, Any]) -> list[str]:
        platform, storage_id = row.get("platform"), row.get("unique_id")
        if platform not in SIMPLE_HELPER_TYPES or not storage_id:
            return []
        owner = collection_owner(self.hass, platform)
        if owner is None:
            return storage_note_unread(
                row["entity_id"], "Core storage collection is unavailable"
            )
        if not any(
            str(item.get("id")) == storage_id
            for item in owner.storage_collection.data.values()
        ):
            return []
        return stored_helper_removal_note(row["entity_id"], platform, storage_id)

    async def remove(self, entity_id: str | list[str]) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            targets, bulk = removal_targets(entity_id)
            if not bulk:
                result = await self._remove_one(targets[0])
                if result is None:
                    raise ToolFailure(
                        "ENTITY_NOT_FOUND",
                        f"Entity '{targets[0]}' not found in registry",
                        status="not_started",
                    )
                return result
            return await self._remove_many(targets)
        except Exception as error:
            raise registry_failure(error, {"entity_id": entity_id}) from error

    async def _remove_many(self, targets: list[str]) -> dict[str, Any]:
        removed, skipped, errors, warnings = [], [], [], []
        uncertain = False
        not_attempted = []
        for index, entity_id in enumerate(targets):
            if index:
                await asyncio.sleep(0)
            try:
                result = await self._remove_one(entity_id)
                if result is None:
                    skipped.append(entity_id)
                else:
                    removed.append(entity_id)
                    warnings.extend(result.get("warnings", []))
            except ToolFailure as error:
                errors.append(
                    {"entity_id": entity_id, "code": error.code, "message": str(error)}
                )
                if error.status == "outcome_unknown":
                    uncertain = True
                    not_attempted = targets[index + 1 :]
                    break
        result = {
            "success": not errors,
            "total": len(targets),
            "removed": removed,
            "skipped": skipped,
            "errors": errors,
        }
        if warnings:
            result["warnings"] = warnings
        if not_attempted:
            result["not_attempted"] = not_attempted
        if errors:
            failure = ToolFailure(
                "OUTCOME_UNKNOWN" if uncertain else "SERVICE_CALL_FAILED",
                "Some entity removals could not be confirmed."
                if uncertain
                else "Some entity removals were refused.",
                status="outcome_unknown"
                if uncertain
                else "confirmed"
                if removed
                else "not_started",
            )
            failure.data.update(result)
            raise failure
        return result

    async def _remove_one(self, entity_id: str) -> dict[str, Any] | None:
        await admit_registry_write(self.actor, self.authorize)
        data = _validated_payload(
            self.hass, "config/entity_registry/remove", {"entity_id": entity_id}
        )
        entity_id = data["entity_id"]
        registry = self._owner()
        entry = registry.async_get(entity_id)
        if entry is None:
            return None
        initial = registry_json(entry.extended_dict)
        identity = entity_registry_identity(initial)
        async with registry_update_lock("entity_registry", identity["id"]):
            await admit_registry_write(self.actor, self.authorize)
            if self._owner() is not registry:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The entity registry owner changed before removal",
                    status="not_started",
                )
            entry = registry.async_get(identity["id"])
            before = registry_json(entry.extended_dict) if entry is not None else None
            if before is None:
                return None
            if (
                not json_values_equal(entity_registry_identity(before), identity)
                or before["entity_id"] != entity_id
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The entity identity changed while waiting for its owner lock",
                    status="not_started",
                )
            notes = self._stored_note(before)
            warnings = await capture_registry_before(
                self.backup,
                self.actor,
                "entity_registry",
                identity["id"],
                before,
                "ha_remove_entity",
            )
            await admit_registry_write(self.actor, self.authorize)
            current = registry.async_get(identity["id"])
            if (
                self._owner() is not registry
                or current is None
                or not json_values_equal(registry_json(current.extended_dict), before)
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The entity changed after its snapshot was read; no removal was sent.",
                    status="not_started",
                )
            check_mutation_admission(self.actor)
            try:
                registry.async_remove(current.entity_id)
                if registry.async_get(identity["id"]) is not None:
                    raise RuntimeError(
                        "Core did not confirm removal of the captured entity identity"
                    )
            except Exception as error:  # noqa: BLE001 - save/listeners can fail after mutation
                registry_outcome_unknown("entity", "remove", error)
            result = {"success": True, "entity_id": entity_id}
            if notes or warnings:
                result["warnings"] = [*notes, *warnings]
            return result

    async def read_snapshot(self, identity: str) -> dict[str, Any] | None:
        await authorize_actor(self.actor, self.authorize)
        entry = self._owner().async_get(identity)
        if entry is None:
            return None
        row = registry_json(entry.extended_dict)
        if row.get("id") != identity:
            raise InvalidBackupSnapshotError(
                "Entity registry snapshot key is not its durable identity"
            )
        return row

    def _references(self, payload: dict[str, Any]) -> None:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import category_registry as cr
        from homeassistant.helpers import label_registry as lr

        if payload.get("area_id"):
            _raise_if_unknown_area(
                [
                    {"area_id": area.id}
                    for area in ar.async_get(self.hass).async_list_areas()
                ],
                payload["area_id"],
            )
        if payload.get("labels"):
            _raise_if_unknown_labels(
                [
                    {"label_id": label.label_id}
                    for label in lr.async_get(self.hass).async_list_labels()
                ],
                payload["labels"],
            )
        for scope, category in payload.get("categories", {}).items():
            if category:
                _raise_if_unknown_category(
                    [
                        {"category_id": item.category_id}
                        for item in cr.async_get(self.hass).async_list_categories(
                            scope=scope
                        )
                    ],
                    category,
                    scope,
                )

    def _write(self, registry: Any, entry: Any, payload: dict[str, Any]) -> Any:
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers import label_registry as lr

        changes = {
            key: value
            for key, value in payload.items()
            if key not in {"entity_id", "options_domain", "options"}
        }
        if "aliases" in changes:
            changes["aliases"] = [
                er.COMPUTED_NAME if alias is None else alias.strip()
                for alias in changes["aliases"]
                if alias is None or alias.strip()
            ]
        if "labels" in changes:
            labels = set(changes["labels"])
            changes["labels"] = labels - lr.async_get_missing_label_ids(
                self.hass, labels
            )
        if "categories" in changes:
            categories = entry.categories.copy()
            for scope, category_id in changes["categories"].items():
                if category_id is None:
                    categories.pop(scope, None)
                else:
                    categories[scope] = category_id
            changes["categories"] = categories
        if changes:
            entry = registry.async_update_entity(entry.entity_id, **changes)
        if "options_domain" in payload:
            entry = registry.async_update_entity_options(
                entry.entity_id, payload["options_domain"], payload["options"]
            )
        return entry

    def _check_enable(self, entry: Any, payload: dict[str, Any]) -> None:
        from homeassistant.helpers import device_registry as dr

        if (
            "disabled_by" in payload
            and payload["disabled_by"] is None
            and entry.device_id
        ):
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device is not None and device.disabled:
                raise ToolFailure(
                    "SERVICE_CALL_FAILED", "Device is disabled", status="not_started"
                )

    def _current_entry(self, registry: Any, identity: dict[str, Any]) -> Any:
        current = registry.async_get(identity["id"])
        if (
            self._owner() is not registry
            or current is None
            or not json_values_equal(
                entity_registry_identity(registry_json(current.extended_dict)),
                identity,
            )
        ):
            raise RuntimeError(
                "The entity registration was replaced or removed by a Core callback"
            )
        return current

    def _confirmed_entry(
        self,
        registry: Any,
        identity: dict[str, Any],
        written: Any,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        current = self._current_entry(registry, identity)
        actual = registry_json(current.extended_dict)
        acknowledged = registry_json(written.extended_dict)
        if not json_values_equal(entity_registry_identity(acknowledged), identity):
            raise RuntimeError("Core acknowledged a different entity identity")
        if payload is None:
            matches = json_values_equal(actual, acknowledged)
        elif "options_domain" in payload:
            domain = payload["options_domain"]
            expected_options = acknowledged["options"].get(domain)
            current_options = actual["options"].get(domain)
            # SensorEntity may synchronously rematerialize its suggested precision.
            # Core owns added fields; an explicitly proposed value may not change.
            matches = (
                current_options is None or isinstance(current_options, dict)
                if expected_options is None
                else (
                    isinstance(current_options, dict)
                    and all(
                        key in current_options
                        and json_values_equal(current_options[key], value)
                        for key, value in expected_options.items()
                    )
                )
            )
        else:
            fields = {
                "entity_id" if key == "new_entity_id" else key for key in payload
            } - {"entity_id"}
            if "new_entity_id" in payload:
                fields.add("entity_id")
            matches = all(
                json_values_equal(actual.get(key), acknowledged.get(key))
                for key in fields
            )
        if not matches:
            raise RuntimeError(
                "An explicitly written entity value changed before confirmation"
            )
        return current

    @staticmethod
    def _effective_options(
        entry: Any, payload: dict[str, Any]
    ) -> tuple[Any, str | None]:
        domain = payload["options_domain"]
        effective = registry_json(entry.extended_dict)["options"].get(domain)
        warning = (
            f"Core enriched options[{domain}]; the response contains the effective options, including fields Core rematerialized."
            if not json_values_equal(effective, payload["options"])
            else None
        )
        return effective, warning

    async def restore_snapshot(
        self, identity: str, config: dict[str, Any], expected: Any
    ) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.authorize)
        if config.get("id") != identity:
            raise InvalidBackupSnapshotError(
                "Entity registry snapshot does not match its durable identity"
            )
        async with registry_update_lock("entity_registry", identity):
            await admit_registry_write(self.actor, self.authorize)
            registry = self._owner()
            entry = registry.async_get(identity)
            if entry is None:
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    "The saved entity registration no longer exists",
                    status="not_started",
                )
            before = registry_json(entry.extended_dict)
            if not json_values_equal(before, expected) or not json_values_equal(
                entity_registry_identity(before), entity_registry_identity(config)
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The entity changed after its restore safety capture",
                    status="not_started",
                )
            payloads = [
                _validated_payload(self.hass, "config/entity_registry/update", payload)
                for payload in entity_restore_payloads(config, before)
            ]
            for payload in payloads:
                self._references(payload)
                self._check_enable(entry, payload)
            check_mutation_admission(self.actor)
            confirmed_phases = []
            effective_options = {}
            warnings = []
            try:
                for payload in payloads:
                    entry = self._current_entry(
                        registry, entity_registry_identity(before)
                    )
                    check_mutation_admission(self.actor)
                    written = self._write(registry, entry, payload)
                    entry = self._confirmed_entry(
                        registry, entity_registry_identity(before), written, payload
                    )
                    confirmed_phases.append(payload.get("options_domain", "registry"))
                    if "options_domain" in payload:
                        effective, warning = self._effective_options(entry, payload)
                        effective_options[payload["options_domain"]] = effective
                        if warning:
                            warnings.append(warning)
                result = registry_json(
                    self._current_entry(
                        registry, entity_registry_identity(before)
                    ).extended_dict
                )
                if warnings:
                    result.update(
                        warnings=warnings, options_succeeded=effective_options
                    )
                return result
            except Exception as error:  # noqa: BLE001 - earlier restore phases may have committed
                registry_outcome_unknown(
                    "entity",
                    "restore",
                    error,
                    context={
                        "partial": bool(confirmed_phases),
                        "confirmed_phases": confirmed_phases,
                    },
                )
