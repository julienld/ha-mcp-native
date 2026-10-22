"""Finite registry operations call installed schemas and synchronous Core owners."""

import importlib
import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.errors import create_validation_error
from ha_mcp.runtime.mutation_guard import check_mutation_admission, current_admission
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

from ..helper_collections import _INVALID_ERRORS
from .command_validation import _validated_payload as _validated_payload
from .registry_failures import registry_outcome_unknown, registry_target_missing


def _category_owners(hass: HomeAssistant) -> tuple[Any, Any]:
    from homeassistant.helpers import category_registry as cr

    module = importlib.import_module(
        "homeassistant.components.config.category_registry"
    )
    serializer = getattr(module, "_entry_dict", None)
    if not callable(serializer):
        raise UnsupportedOperation("registry.category.serializer")
    return cr.async_get(hass), serializer


def _category_row(registry: Any, serialize: Any, scope: str, category_id: str) -> Any:
    entry = registry.async_get_category(scope=scope, category_id=category_id)
    return json.loads(json_bytes(serialize(entry))) if entry is not None else None


async def category_read(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    registry, serializer = _category_owners(hass)
    return _category_row(
        registry, serializer, arguments["scope"], arguments["category_id"]
    )


async def _category_mutation(
    hass: HomeAssistant, arguments: dict[str, Any], action: str
) -> dict[str, Any]:
    data = _validated_payload(
        hass, f"config/category_registry/{action}", arguments["data"]
    )
    registry, serializer = _category_owners(hass)
    before = (
        _category_row(registry, serializer, data["scope"], data["category_id"])
        if action != "create"
        else None
    )
    if not json_values_equal(before, arguments["expected_item"]):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The category changed after its snapshot was read; no mutation was sent.",
            status="not_started",
        )
    if action != "create" and before is None:
        registry_target_missing("category", data["category_id"])
    validate_name = getattr(registry, "_async_ensure_name_is_available", None)
    if action in {"create", "update"} and "name" in data and callable(validate_name):
        try:
            if action == "create":
                validate_name(data["scope"], data["name"])
            else:
                validate_name(data["scope"], data["name"], data["category_id"])
        except _INVALID_ERRORS as error:
            payload = create_validation_error(str(error))
            raise ToolFailure(
                payload["error"]["code"],
                payload["error"]["message"],
                data=payload,
                status="not_started",
            ) from error
    operation = getattr(registry, f"async_{action}", None)
    if not callable(operation):
        raise UnsupportedOperation(f"registry.category.{action}")
    if admission := current_admission.get():
        check_mutation_admission(admission.context)
    # Schema, expected image and callback are synchronous on Core's loop.
    # NativeBackend has just revalidated actor and write admission.
    try:
        entry = operation(**data)
        return {
            "success": True,
            "result": serializer(entry) if action != "delete" else None,
        }
    except Exception as error:  # noqa: BLE001 - callback/save/listener may already have mutated
        registry_outcome_unknown("category", action, error)


async def category_create(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _category_mutation(hass, arguments, "create")


async def category_update(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _category_mutation(hass, arguments, "update")


async def category_delete(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _category_mutation(hass, arguments, "delete")


DIRECT_OPERATIONS = {
    "registry.category.read": (category_read, False),
    "registry.category.create": (category_create, True),
    "registry.category.update": (category_update, True),
    "registry.category.delete": (category_delete, True),
}
