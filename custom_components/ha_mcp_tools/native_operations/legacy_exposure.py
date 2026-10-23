"""Preferences for entities without a registry entry, owned by Core's real store.

The small replace adapter is audited against Core 2026.10.2. Core exposes only
merge setters, so restoring absence uses its own ExposedEntity model, actual
entities mapping, save scheduler and assistant listeners. No state is created.
"""

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from pydantic import TypeAdapter, ValidationError

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.snapshot_payloads import InvalidBackupSnapshotError
from ha_mcp.runtime.validation import validate_identifier_not_empty
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

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
class NativeLegacyExposure:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    backup: Capture | None = field(default=None, kw_only=True, repr=False)

    def _owner(self) -> tuple[Any, Any]:
        from homeassistant.components.homeassistant import exposed_entities as module

        owner = self.hass.data.get(getattr(module, "DATA_EXPOSED_ENTITIES", None))
        if type(owner) is not getattr(
            module, "ExposedEntities", None
        ) or not isinstance(getattr(owner, "entities", None), dict):
            raise UnsupportedOperation("entity_exposure.core_owner")
        return owner, module

    def _restore_owner(self) -> tuple[Any, Any]:
        """Only exact replacement depends on Core's private persistence protocol."""
        owner, module = self._owner()
        if (
            not isinstance(getattr(owner, "_listeners", None), dict)
            or not callable(getattr(owner, "_async_schedule_save", None))
            or not dataclasses.is_dataclass(getattr(module, "ExposedEntity", None))
        ):
            raise UnsupportedOperation("entity_exposure.core_owner")
        return owner, module

    def _require_legacy(self, entity_id: str) -> None:
        from homeassistant.helpers import entity_registry as er

        if er.async_get(self.hass).async_get(entity_id) is not None:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "This entity now has a registry entry; its legacy exposure preferences are a different owner.",
                status="not_started",
            )

    @staticmethod
    def _snapshot(owner: Any, entity_id: str) -> dict[str, Any]:
        row = owner.entities.get(entity_id)
        return {
            "entity_id": entity_id,
            "preferences": registry_json(row.to_json()) if row is not None else None,
        }

    def _check_current(
        self, owner: Any, entity_id: str, expected: dict[str, Any]
    ) -> None:
        current_owner, _ = self._owner()
        self._require_legacy(entity_id)
        if current_owner is not owner or not json_values_equal(
            self._snapshot(owner, entity_id), expected
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Legacy exposure preferences changed after their before-image was read.",
                status="not_started",
            )

    async def set(self, entity_id: str, exposure: dict[str, bool]) -> dict[str, Any]:
        try:
            await admit_registry_write(self.actor, self.authorize)
            validate_identifier_not_empty(entity_id, "entity_id")
            async with registry_update_lock("entity_exposure", entity_id):
                self._require_legacy(entity_id)
                owner, module = self._owner()
                before = self._snapshot(owner, entity_id)
                groups = [
                    _validated_payload(
                        self.hass,
                        "homeassistant/expose_entity",
                        {
                            "entity_ids": [entity_id],
                            "assistants": [assistant],
                            "should_expose": value,
                        },
                    )
                    for assistant, value in exposure.items()
                ]
                groups.sort(key=lambda group: not group["should_expose"])
                if not groups:
                    return {"success": True, "entity_id": entity_id, "exposure": {}}
                warnings = await capture_registry_before(
                    self.backup,
                    self.actor,
                    "entity_exposure",
                    entity_id,
                    before,
                    "ha_set_entity",
                )
                await admit_registry_write(self.actor, self.authorize)
                self._check_current(owner, entity_id, before)
                return self._apply_exposure(module, owner, entity_id, groups, warnings)
        except Exception as error:
            raise registry_failure(error, {"entity_id": entity_id}) from error

    def _apply_exposure(
        self,
        module: Any,
        owner: Any,
        entity_id: str,
        groups: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, Any]:
        succeeded: dict[str, bool] = {}
        for group in groups:
            for assistant in group["assistants"]:
                try:
                    check_mutation_admission(self.actor)
                    self._require_legacy(entity_id)
                    current_owner, _ = self._owner()
                    if current_owner is not owner:
                        raise ToolFailure(
                            "CONFIG_CONFLICT",
                            "The Core exposure owner changed",
                            status="not_started",
                        )
                except Exception as error:
                    failure = registry_failure(error, {"entity_id": entity_id})
                    if succeeded:
                        failure.status = "confirmed"
                        failure.data.update(
                            {"partial": True, "exposure_succeeded": succeeded}
                        )
                    raise failure from error
                try:
                    module.async_expose_entity(
                        self.hass, assistant, entity_id, group["should_expose"]
                    )
                    self._require_legacy(entity_id)
                    current_owner, _ = self._owner()
                    written = self._snapshot(owner, entity_id)
                    actual = (
                        (written["preferences"] or {})
                        .get("assistants", {})
                        .get(assistant, {})
                        .get("should_expose")
                    )
                    if (
                        current_owner is not owner
                        or actual is not group["should_expose"]
                    ):
                        raise RuntimeError(
                            "Core did not confirm the legacy exposure option"
                        )
                except Exception as error:  # noqa: BLE001 - Core save/listeners may fail after commit
                    registry_outcome_unknown(
                        "entity_exposure",
                        "set",
                        error,
                        context={
                            "partial": bool(succeeded),
                            "exposure_succeeded": dict(succeeded),
                        },
                    )
                succeeded[assistant] = group["should_expose"]
        result = {
            "success": True,
            "entity_id": entity_id,
            "exposure": succeeded,
            "entity_entry": None,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    async def read_snapshot(self, entity_id: str) -> dict[str, Any]:
        await authorize_actor(self.actor, self.authorize)
        self._require_legacy(entity_id)
        owner, _ = self._owner()
        return self._snapshot(owner, entity_id)

    @staticmethod
    def _restore_value(module: Any, config: dict[str, Any], entity_id: str) -> Any:
        if config.get("entity_id") != entity_id or "preferences" not in config:
            raise InvalidBackupSnapshotError(
                "Legacy exposure snapshot does not match its entity identity"
            )
        raw = config["preferences"]
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) - {
            item.name for item in dataclasses.fields(module.ExposedEntity)
        }:
            raise InvalidBackupSnapshotError(
                "Legacy exposure preferences do not match the installed Core model"
            )
        try:
            return TypeAdapter(module.ExposedEntity).validate_python(raw)
        except (ValidationError, TypeError, ValueError) as error:
            raise InvalidBackupSnapshotError(
                "Legacy exposure preferences do not match the installed Core model"
            ) from error

    async def restore_snapshot(
        self, entity_id: str, config: dict[str, Any], expected: dict[str, Any]
    ) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.authorize)
        async with registry_update_lock("entity_exposure", entity_id):
            await admit_registry_write(self.actor, self.authorize)
            owner, module = self._restore_owner()
            restored = self._restore_value(module, config, entity_id)
            self._check_current(owner, entity_id, expected)
            current = self._snapshot(owner, entity_id)
            if json_values_equal(current, config):
                return current
            affected = set((current["preferences"] or {}).get("assistants", {})) | set(
                restored.assistants if restored is not None else ()
            )
            check_mutation_admission(self.actor)
            try:
                if restored is None:
                    owner.entities.pop(entity_id, None)
                else:
                    owner.entities[entity_id] = restored
                owner._async_schedule_save()
                for assistant in affected:
                    for listener in owner._listeners.get(assistant, []):
                        listener()
                self._require_legacy(entity_id)
                current_owner, _ = self._owner()
                written = self._snapshot(owner, entity_id)
                if current_owner is not owner or not json_values_equal(written, config):
                    raise RuntimeError(
                        "Core preferences changed during restore listeners"
                    )
                return written
            except Exception as error:  # noqa: BLE001 - mapping/save/listeners can already have changed
                registry_outcome_unknown("entity_exposure", "restore", error)
