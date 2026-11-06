"""Bounded removals delegate to the real Core collection and config-entry owners."""

import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes

from ha_mcp.errors import HomeAssistantAPIError
from ha_mcp.runtime.flows.entry import refuse_component_entry
from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.storage_targets import raise_not_stored
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io

from ..helper_collections import _INVALID_ERRORS, collection_owner
from .helpers import check_expected_item
from .owner import CoreOperationOwner
from .registry_expectation import check_expected_registry


async def helper_entity(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.helpers import config_validation as cv

    try:
        entity_id = cv.entity_id(arguments["entity_id"])
    except _INVALID_ERRORS as error:
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER", str(error), status="not_started"
        ) from error
    entry = er.async_get(hass).async_get(entity_id)
    return {
        "entry": entry.extended_dict if entry is not None else None,
        "reason": "ok"
        if entry is not None
        else "not_registry_managed"
        if hass.states.get(entity_id) is not None
        else "not_in_registry",
        "warnings": [],
    }


async def storage_target(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    helper_type, target = arguments["helper_type"], arguments["target"]
    if helper_type not in SIMPLE_HELPER_TYPES:
        raise UnsupportedOperation(f"helpers.storage.{helper_type}")
    owner = collection_owner(hass, helper_type)
    if owner is None:
        raise UnsupportedOperation(f"helpers.storage.{helper_type}")
    if target not in owner.storage_collection.data:
        if target.startswith(f"{helper_type}."):
            return None
        raise_not_stored(helper_type, target)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(helper_type, helper_type, target)
    return (
        target,
        entity_id,
        None if entity_id else f"the {helper_type} has no entity in the registry",
    )


async def registry_target(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await helper_entity(hass, arguments)
    entry = result["entry"]
    return {
        "unique_id": entry.get("unique_id") if entry else None,
        "registry_result": {"success": True, "result": entry}
        if entry
        else {"success": False, "error_code": "not_found"},
        "component_used": True,
    }


DIRECT_OPERATIONS = {
    "helpers.remove.entity": (helper_entity, False),
    "helpers.remove.storage_target": (storage_target, False),
    "helpers.remove.registry_target": (registry_target, False),
}
REMOVAL_OPERATIONS = frozenset(
    {"helpers.storage.remove", "config.entries.remove", "config.subentries.remove"}
)


class NativeRemovals(CoreOperationOwner):
    async def execute(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        return await await_owned_io(self._execute_admitted(operation, arguments, actor))

    async def _execute_admitted(
        self, operation: str, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        try:
            await self.authorize(actor)
        except PermissionError as error:
            raise ToolFailure(
                "AUTH_REQUIRED", "Removal admission was revoked.", status="not_started"
            ) from error
        check_mutation_admission(actor)
        if actor.read_only:
            raise ToolFailure("READ_ONLY", "Writes are disabled.", status="not_started")
        if operation == "helpers.storage.remove":
            return await self._storage(arguments)
        if operation in {"config.entries.remove", "config.subentries.remove"}:
            return await self._entry(operation, arguments)
        raise UnsupportedOperation(operation)

    async def _storage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments["helper_type"] not in SIMPLE_HELPER_TYPES:
            raise UnsupportedOperation(f"helpers.storage.{arguments['helper_type']}")
        owner = collection_owner(self.hass, arguments["helper_type"])
        if owner is None:
            raise UnsupportedOperation("helpers.storage.remove")
        check_expected_item(self.hass, arguments, arguments["expected_item"])
        check_expected_registry(self.hass, arguments.get("expected_registry"))
        try:
            await owner.storage_collection.async_delete_item(arguments["item_id"])
        except Exception as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The helper removal was dispatched but did not complete cleanly.",
                status="outcome_unknown",
            ) from error
        return {"success": True}

    async def _entry(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from homeassistant.config_entries import UnknownEntry, UnknownSubEntry

        entry_id = arguments["entry_id"]
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise HomeAssistantAPIError("Config entry not found", status_code=404)
        refuse_component_entry(
            entry.domain, action="delete a config entry of", entry_id=entry_id
        )
        if not json_values_equal(
            json.loads(json_bytes(entry.as_json_fragment)), arguments["expected_entry"]
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The integration changed after its removal snapshot.",
                status="not_started",
            )
        try:
            if operation == "config.subentries.remove":
                self.hass.config_entries.async_remove_subentry(
                    entry, arguments["subentry_id"]
                )
                return {"success": True}
            result: dict[str, Any] = await self.hass.config_entries.async_remove(
                entry_id
            )
            return result
        except UnknownEntry as error:
            raise HomeAssistantAPIError(
                "Config entry not found", status_code=404
            ) from error
        except UnknownSubEntry:
            return {
                "success": False,
                "error": {"code": "not_found", "message": "Config subentry not found"},
            }
        except Exception as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "The integration removal was dispatched but did not complete cleanly.",
                status="outcome_unknown",
            ) from error
