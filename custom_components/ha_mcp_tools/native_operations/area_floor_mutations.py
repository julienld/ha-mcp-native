"""Area/floor writes use Core's schemas, registry callbacks and serializers."""

import importlib
import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.mutation_guard import check_mutation_admission, current_admission
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

from .registry_failures import registry_outcome_unknown, registry_target_missing
from .registry_mutations import _validated_payload


def _area_serializer(entry: Any) -> Any:
    return entry.json_fragment


def _owners(hass: HomeAssistant, domain: str) -> tuple[Any, Any]:
    helpers = importlib.import_module(f"homeassistant.helpers.{domain}_registry")
    registry = helpers.async_get(hass)
    serialize: Any
    if domain == "area":
        serialize = _area_serializer
    else:
        module = importlib.import_module(
            "homeassistant.components.config.floor_registry"
        )
        serialize = getattr(module, "_entry_dict", None)
        if not callable(serialize):
            raise UnsupportedOperation("registry.floor.serializer")
    return registry, serialize


def _row(hass: HomeAssistant, domain: str, identifier: str) -> Any:
    registry, serialize = _owners(hass, domain)
    entry = getattr(registry, f"async_get_{domain}")(identifier)
    return json.loads(json_bytes(serialize(entry))) if entry is not None else None


def _owner_payload(
    hass: HomeAssistant, domain: str, data: dict[str, Any]
) -> dict[str, Any]:
    # Core's frontend adapters convert API lists to registry-owned sets.
    if "aliases" in data:
        data["aliases"] = {
            stripped for value in data["aliases"] if (stripped := value.strip())
        }
    if domain == "area" and "labels" in data:
        from homeassistant.helpers import label_registry as lr

        labels = set(data["labels"])
        data["labels"] = labels - lr.async_get_missing_label_ids(hass, labels)
    return data


async def _mutate(
    hass: HomeAssistant, arguments: dict[str, Any], domain: str, action: str
) -> dict[str, Any]:
    data = _validated_payload(
        hass, f"config/{domain}_registry/{action}", arguments["data"]
    )
    registry, serialize = _owners(hass, domain)
    before = _row(hass, domain, data[f"{domain}_id"]) if action != "create" else None
    if not json_values_equal(before, arguments["expected_item"]):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The registry entry changed after its snapshot was read; no mutation was sent.",
            status="not_started",
        )
    if action != "create" and before is None:
        registry_target_missing(domain, data[f"{domain}_id"])
    data = _owner_payload(hass, domain, data)
    operation = getattr(registry, f"async_{action}", None)
    if not callable(operation):
        raise UnsupportedOperation(f"registry.{domain}.{action}")
    if admission := current_admission.get():
        check_mutation_admission(admission.context)
    try:
        entry = operation(**data)
        return {
            "success": True,
            "result": json.loads(json_bytes(serialize(entry)))
            if action != "delete"
            else "success"
            if domain == "area"
            else None,
        }
    except Exception as error:  # noqa: BLE001 - callback/save/listener may already have mutated
        registry_outcome_unknown(domain, action, error)


async def area_read(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    return _row(hass, "area", arguments["area_id"])


async def floor_read(hass: HomeAssistant, arguments: dict[str, Any]) -> Any:
    return _row(hass, "floor", arguments["floor_id"])


async def area_create(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    return await _mutate(hass, arguments, "area", "create")


async def floor_create(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _mutate(hass, arguments, "floor", "create")


async def area_update(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    return await _mutate(hass, arguments, "area", "update")


async def floor_update(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _mutate(hass, arguments, "floor", "update")


async def area_delete(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    return await _mutate(hass, arguments, "area", "delete")


async def floor_delete(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return await _mutate(hass, arguments, "floor", "delete")


DIRECT_OPERATIONS = {
    "registry.area.read": (area_read, False),
    "registry.floor.read": (floor_read, False),
    "registry.area.create": (area_create, True),
    "registry.floor.create": (floor_create, True),
    "registry.area.update": (area_update, True),
    "registry.floor.update": (floor_update, True),
    "registry.area.delete": (area_delete, True),
    "registry.floor.delete": (floor_delete, True),
}
