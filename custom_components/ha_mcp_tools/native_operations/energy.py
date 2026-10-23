"""Energy preferences through their installed Core manager and validators."""

import asyncio
import copy
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.energy_changes import EnergyChanges
from ha_mcp.runtime.energy_values import (
    _check_config_hash,
    _flatten_validation_errors,
    _statistic_ids,
    compute_per_key_hashes,
)
from ha_mcp.runtime.statistics_results import statistics_unit
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import compute_config_hash, json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..core_contract import describe_contract, validate_request
from .command_validation import _validated_payload
from .registry_access import (
    Authorize,
    admit_registry_write,
    authorize_actor,
    registry_failure,
    registry_json,
)

_COMMAND = "energy/save_prefs"
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class NativeEnergy(EnergyChanges):
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)

    async def _owner(self) -> Any:
        await authorize_actor(self.actor, self.authorize)
        if _COMMAND not in self.hass.data.get("websocket_api", {}):
            raise ToolFailure(
                "SERVICE_UNAVAILABLE", "Core Energy is not loaded", status="not_started"
            )
        owner = self.hass.data.get("energy_manager")
        if owner is None or isinstance(owner, asyncio.Future):
            # Core's sensor platform owns initialization and possible storage
            # migrations. A read must neither start it nor cancel its Future.
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Core Energy is still initializing; retry after Core startup completes",
                status="not_started",
            )
        return owner

    def _pin(self, owner: Any, registration: Any) -> None:
        # Core's original async singleton publishes this manager after loading.
        if (
            self.hass.data.get("energy_manager") is not owner
            or self.hass.data.get("websocket_api", {}).get(_COMMAND) is not registration
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT", "Core Energy owner changed", status="not_started"
            )

    @staticmethod
    def _prefs(owner: Any) -> dict[str, Any]:
        return copy.deepcopy(
            owner.data if owner.data is not None else owner.default_preferences()
        )

    async def read(
        self, *, include_schema: bool, include_statistics: bool
    ) -> dict[str, Any]:
        owner = await self._owner()
        prefs = self._prefs(owner)
        result = {
            "success": True,
            "mode": "get",
            "config": prefs,
            "config_hash": compute_config_hash(prefs),
            "config_hash_per_key": compute_per_key_hashes(prefs),
        }
        if owner.data is None:
            result["note"] = "Energy Dashboard has never been configured."
        if include_schema:
            contract = describe_contract(self.hass, _COMMAND)
            contract["default_preferences"] = copy.deepcopy(owner.default_preferences())
            warnings = contract.pop("warnings", [])
            if warnings:
                result["warnings"] = warnings
            result["core_contract"] = contract
        if include_statistics:
            await self._statistics(result)
        return result

    async def _statistics(self, result: dict[str, Any]) -> None:
        from homeassistant.components.recorder.statistics import (
            async_list_statistic_ids,
        )
        from homeassistant.helpers.json import json_bytes

        identifiers = sorted(_statistic_ids(result["config"]))
        metadata: dict[str, dict[str, Any]] = {}
        failure = None
        if identifiers:
            try:
                rows = await await_owned_io(
                    async_list_statistic_ids(self.hass, set(identifiers))
                )
                detached: list[dict[str, Any]] = json.loads(json_bytes(rows))
                metadata = {row["statistic_id"]: row for row in detached}
            except Exception:
                _LOGGER.warning(
                    "Recorder statistics metadata lookup failed", exc_info=True
                )
                failure = "statistics_metadata_unavailable"
        await authorize_actor(self.actor, self.authorize)
        result["statistics_metadata"] = [
            {
                **(metadata.get(identifier) or {"statistic_id": identifier}),
                **statistics_unit(metadata.get(identifier), failure),
            }
            for identifier in identifiers
        ]
        warnings = [
            f"{row['statistic_id']}: {row['unit_reason']}"
            for row in result["statistics_metadata"]
            if row["unit_source"] == "unknown"
        ]
        if warnings:
            result.setdefault("warnings", []).extend(warnings)

    async def _validation(self) -> tuple[list[dict[str, Any]], str | None, Any]:
        from homeassistant.components.energy.validate import async_validate

        try:
            validation = registry_json((await async_validate(self.hass)).as_dict())
            return _flatten_validation_errors(validation), None, validation
        except Exception as error:
            _LOGGER.warning("Core Energy semantic validation failed", exc_info=True)
            return [], str(error), None

    async def preview(self, config: dict[str, Any]) -> dict[str, Any]:
        await self._owner()
        # Validation only: never call the manager's write or acquire a snapshot.
        self._validated_config(config)
        proposal = validate_request(self.hass, _COMMAND, config)
        errors, failure, validation = await await_owned_io(self._validation())
        await authorize_actor(self.actor, self.authorize)
        result: dict[str, Any] = {
            "success": True,
            "mode": "set",
            "dry_run": True,
            "proposal_validation": proposal,
            "current_state_validation_errors": errors,
            "current_state_validation": validation,
            "message": "No preferences saved. Semantic checks describe the current persisted state only.",
        }
        if failure:
            result.update(
                partial=True, warnings=[f"Current-state validation failed: {failure}"]
            )
        return result

    def _validated_config(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            return _validated_payload(self.hass, _COMMAND, config)
        except ToolFailure as error:
            if error.code != "VALIDATION_FAILED":
                raise
            message = f"Core rejected Energy preferences: {error}"
            details = error.data["error"]
            assert isinstance(details, dict)
            payload = {
                **error.data,
                "error": {**details, "message": message},
            }
            raise ToolFailure(
                error.code, message, data=payload, status=error.status
            ) from error

    async def save(
        self, config: dict[str, Any], config_hash: str | dict[str, str]
    ) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.authorize)
        async with registry_update_lock("energy", "preferences"):
            owner = await self._owner()
            return await self._save(owner, config, config_hash)

    async def _save(
        self, owner: Any, config: dict[str, Any], config_hash: str | dict[str, str]
    ) -> dict[str, Any]:
        before = copy.deepcopy(owner.data)
        _check_config_hash(config, config_hash, self._prefs(owner))
        registration = self.hass.data["websocket_api"][_COMMAND]
        payload = self._validated_config(config)
        return await await_owned_io(self._commit(owner, registration, before, payload))

    async def _commit(
        self, owner: Any, registration: Any, before: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        # The worker owns this check; creating a task must not separate admission
        # from Core's first mutation. Core updates data before awaiting listeners.
        await admit_registry_write(self.actor, self.authorize)
        self._pin(owner, registration)
        if not json_values_equal(owner.data, before):
            raise ToolFailure(
                "RESOURCE_LOCKED",
                "Energy preferences changed before saving",
                status="not_started",
            )
        expected = self._prefs(owner)
        normalized = copy.deepcopy(payload)
        if "energy_sources" in normalized:
            normalized["energy_sources"] = owner._process_energy_sources(
                normalized["energy_sources"]
            )
        expected.update(normalized)
        # The normalizer can reuse source dictionaries. Neither the Core writer
        # nor its listeners may share nested objects with our confirmation image.
        expected = copy.deepcopy(expected)
        try:
            await owner.async_update(copy.deepcopy(payload))
            self._pin(owner, registration)
            if not json_values_equal(owner.data, expected):
                raise RuntimeError("Energy preferences changed during Core callbacks")
            saved = copy.deepcopy(owner.data)
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Core Energy save was dispatched; its final outcome is uncertain. Re-read before another change.",
                status="outcome_unknown",
            ) from error
        errors, failure, validation = await self._validation()
        await self._confirm_observation(owner, registration, saved)
        result: dict[str, Any] = {
            "success": True,
            "mode": "set",
            "post_save_validation": validation,
            "config": saved,
            "config_hash": compute_config_hash(saved),
            "config_hash_per_key": compute_per_key_hashes(saved),
            "message": "Energy prefs updated.",
        }
        if errors:
            result["post_save_validation_errors"] = errors
            result["warnings"] = [
                f"Save succeeded, but the persisted config has {len(errors)} validation error(s). Review and re-write if any relate to this change."
            ]
        elif failure:
            result.update(
                partial=True,
                warnings=[
                    f"Save succeeded, but post-save energy/validate failed: {failure}. The persisted config has not been re-validated."
                ],
            )
        return result

    async def _confirm_observation(
        self, owner: Any, registration: Any, saved: dict[str, Any]
    ) -> None:
        try:
            await authorize_actor(self.actor, self.authorize)
            self._pin(owner, registration)
            if not json_values_equal(owner.data, saved):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "Energy preferences changed while validating the completed save",
                )
        except Exception as error:
            failure = registry_failure(error, {"operation": "energy_preferences"})
            raise ToolFailure(
                failure.code,
                str(failure),
                data={
                    **failure.data,
                    "write_completed": True,
                    "operation_outcome": "confirmed",
                    "requires_reread": True,
                    "warnings": [
                        "Core completed this save. Its response is withheld because authority or the observed preferences changed during validation; do not replay the write."
                    ],
                },
                status="confirmed",
            ) from error

    async def change(
        self,
        *,
        mode: str,
        target_key: str,
        mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        dry_run: bool,
        preview_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not dry_run:
            await admit_registry_write(self.actor, self.authorize)
        async with registry_update_lock("energy", "preferences"):
            owner = await self._owner()
            current = self._prefs(owner)
            existing = list(current.get(target_key, []))
            updated = mutator(existing)
            payload = {target_key: updated}
            if dry_run:
                return {
                    **await self.preview(payload),
                    "mode": mode,
                    **preview_payload,
                    "current_count": len(existing),
                    "new_count": len(updated),
                }
            saved = await self._save(owner, payload, compute_config_hash(current))
            return {
                **saved,
                "mode": mode,
                "target_key": target_key,
                "new_count": len(updated),
            }
