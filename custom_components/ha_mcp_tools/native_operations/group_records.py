"""Values and installed Core capabilities for old-style group entities."""

from typing import Any

from ha_mcp.errors import create_validation_error
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

from ..helper_collections import _INVALID_ERRORS


def component(hass: Any) -> Any:
    if "set" not in hass.services.async_services().get("group", {}):
        raise UnsupportedOperation("groups.core_component")
    from homeassistant.components.group.const import DATA_COMPONENT

    owner = hass.data.get(DATA_COMPONENT)
    if owner is None or not callable(getattr(owner, "get_entity", None)):
        raise UnsupportedOperation("groups.core_component")
    return owner


def target(component: Any, entity_id: str) -> Any:
    from homeassistant.components.group.entity import Group

    owner = component.get_entity(entity_id)
    if owner is not None and type(owner) is not Group:
        raise UnsupportedOperation("groups.core_group_type")
    return owner


def group_snapshot(owner: Any) -> dict[str, Any] | None:
    if owner is None:
        return None
    if owner.mode not in (any, all):
        raise UnsupportedOperation("groups.mode")
    return {
        "object_id": owner.entity_id.removeprefix("group."),
        "name": owner.name,
        "entities": list(owner.tracking),
        "icon": owner.icon,
        "all": owner.mode is all,
        "created_by_service": owner.created_by_service,
        "order": owner.extra_state_attributes.get("order"),
    }


def public_snapshot(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: value[key] for key in ("object_id", "name", "entities", "icon", "all")}


def validate_service(hass: Any, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    registration = hass.services.async_services().get("group", {}).get(action)
    schema = getattr(registration, "schema", None)
    if not callable(schema):
        raise UnsupportedOperation(f"groups.{action}.validator")
    try:
        result: dict[str, Any] = schema(payload)
    except _INVALID_ERRORS as error:
        data = create_validation_error(str(error))
        raise ToolFailure(
            data["error"]["code"], str(error), data=data, status="not_started"
        ) from error
    return result


def compare_target(
    hass: Any, captured: Any, entity_id: str, owner: Any, expected: Any
) -> None:
    current = component(hass)
    if (
        current is not captured
        or target(current, entity_id) is not owner
        or not json_values_equal(group_snapshot(owner), expected)
    ):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The group changed after its before-image was captured",
            status="not_started",
        )


def assert_group_values(owner: Any, expected: dict[str, Any]) -> None:
    actual = group_snapshot(owner)
    if actual is None or any(
        set(actual[key]) != set(value)
        if key == "entities"
        else not json_values_equal(actual[key], value)
        for key, value in expected.items()
    ):
        raise RuntimeError("Core group callback did not retain the acknowledged values")
