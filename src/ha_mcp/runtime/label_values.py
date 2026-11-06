"""Pure label parsing, merge and projection rules without an execution backend."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.coercion import parse_string_list_param

from .errors import raise_failure

PARTIAL_RETRY_SUGGESTIONS = [
    (
        "The label write already succeeded; retry only the remaining area "
        + "IDs (do not recreate the label)."
    ),
    (
        "Use ha_set_area_or_floor(kind='area', labels=...) to replace an "
        + "area's label set, or ha_list_floors_areas() to inspect current "
        + "assignments."
    ),
]


def string_labels(entry: dict[str, Any] | None) -> list[str]:
    """Return the string label IDs stored on a registry entry."""
    if not isinstance(entry, dict):
        return []
    return [lbl for lbl in (entry.get("labels") or []) if isinstance(lbl, str)]


def area_label_addition(area: dict[str, Any] | None, label_id: str) -> list[str] | None:
    current = string_labels(area)
    return None if label_id in current else [*current, label_id]


def unique_area_ids(area_ids: list[str]) -> list[str]:
    """Reject empty IDs and return unique area IDs in caller order."""
    seen: set[str] = set()
    unique: list[str] = []
    for area_id in area_ids:
        if not area_id:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "areas must be a list of non-empty area IDs",
                    context={"areas": area_ids},
                    suggestions=[
                        "Use ha_list_floors_areas() to list valid area IDs.",
                        "Omit areas to leave area assignments unchanged.",
                    ],
                )
            )
        if area_id not in seen:
            seen.add(area_id)
            unique.append(area_id)
    return unique


def parse_areas(areas: str | list[str] | None) -> list[str] | None:
    try:
        return parse_string_list_param(areas, "areas")
    except ValueError as e:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid areas parameter: {e}",
            )
        )
    return None  # py/mixed-returns: explicit terminal; raise_tool_error is NoReturn


def require_area_ids(area_ids: list[str], known: set[str]) -> None:
    unknown = [identifier for identifier in area_ids if identifier not in known]
    if unknown:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "areas contains area IDs that do not exist in the area registry: "
                + ", ".join(repr(value) for value in unknown),
                context={"areas": area_ids, "unknown_area_ids": unknown},
                suggestions=[
                    "Use ha_list_floors_areas() to list valid area IDs.",
                    "Omit areas to leave area assignments unchanged.",
                    f"Available area_ids: {sorted(known)}",
                ],
            )
        )


def with_retry_suggestions(parsed: dict[str, Any]) -> dict[str, Any]:
    original = parsed["error"]
    existing = original.get("suggestions") or (
        [original["suggestion"]] if original.get("suggestion") else []
    )
    combined = [*existing, *PARTIAL_RETRY_SUGGESTIONS]
    return {**original, "suggestion": combined[0], "suggestions": combined}


def label_get_result(
    labels: list[dict[str, Any]], label_id: str | None
) -> dict[str, Any]:
    if label_id is None:
        return {
            "success": True,
            "count": len(labels),
            "labels": labels,
            "message": f"Found {len(labels)} label(s)",
        }

    label = next((lbl for lbl in labels if lbl.get("label_id") == label_id), None)

    if label:
        return {
            "success": True,
            "label_id": label_id,
            "label": label,
            "message": f"Found label: {label.get('name', label_id)}",
        }
    else:
        available_ids = [lbl.get("label_id") for lbl in labels[:10]]
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Label not found: {label_id}",
                context={
                    "label_id": label_id,
                    "available_label_ids": available_ids,
                },
                suggestions=[
                    "Use ha_config_get_label() without label_id to see all labels"
                ],
            )
        )


def label_set_result(
    name: str,
    action: str,
    data: dict[str, Any],
    label_id: str | None,
    assigned: list[str] | None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "label_id": data.get("label_id") or label_id,
        "label_data": data,
        "message": f"Successfully {'created' if action == 'create' else 'updated'} label: {name}",
    }
    if assigned is not None:
        result["assigned_areas"] = assigned
    if warnings:
        result["warnings"] = warnings
    return result


def label_remove_result(label_id: str) -> dict[str, Any]:
    return {
        "success": True,
        "label_id": label_id,
        "message": f"Successfully deleted label: {label_id}",
    }
