"""Device-write argument normalization and result presentation without Core I/O."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.runtime.validation import clearable_value
from ha_mcp.tools.coercion import parse_string_list_param


def device_update_values(
    device_id: str,
    name: str | None,
    area_id: str | None,
    disabled_by: str | None,
    labels: str | list[str] | None,
) -> tuple[dict[str, Any], list[str]]:
    payload: dict[str, Any] = {"device_id": device_id}
    updates = []
    if name is not None:
        name = clearable_value(name, "name")
        payload["name_by_user"] = name
        updates.append(f"name='{name}'" if name else "name cleared")
    if area_id is not None:
        area_id = clearable_value(area_id, "area_id")
        payload["area_id"] = area_id
        updates.append(f"area_id='{area_id}'" if area_id else "area cleared")
    if disabled_by is not None:
        disabled_by = clearable_value(
            disabled_by,
            "disabled_by",
            hint="Pass 'user' to disable the device, or '' (a single space "
            "' ' if your client cannot send an empty string) to enable it",
        )
        payload["disabled_by"] = disabled_by
        updates.append(f"disabled_by='{disabled_by}'" if disabled_by else "enabled")
    if labels is not None:
        try:
            payload["labels"] = parse_string_list_param(labels, "labels")
        except ValueError as error:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid labels parameter: {error}",
                )
            )
        updates.append(f"labels={payload['labels']}")
    if not updates:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "No updates specified",
                suggestions=[
                    "Provide at least one of: name, area_id, disabled_by, or labels"
                ],
                context={"device_id": device_id},
            )
        )
    return payload, updates


def device_update_result(
    device_id: str,
    row: dict[str, Any],
    updates: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    result = {
        "success": True,
        "device_id": device_id,
        "updates": updates,
        "device_entry": {**row, "name": row.get("name_by_user") or row.get("name")},
        "message": f"Device updated: {', '.join(updates)}",
        "note": "Remember: Device rename does NOT cascade to entities. Use ha_set_entity(new_entity_id=...) to rename entities.",
    }
    if warnings:
        result["warnings"] = warnings
    return result
