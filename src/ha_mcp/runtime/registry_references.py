"""Shared registry-reference validation policy over authoritative registry rows."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def _registry_id_values(items: list[dict[str, Any]], field: str) -> list[str]:
    """Pull non-empty string values of `field` from a list of registry dicts."""
    return [
        v
        for it in items
        if isinstance(it, dict) and isinstance((v := it.get(field)), str)
    ]


def _raise_if_unknown_labels(
    ws_labels: list[dict[str, Any]], labels: list[str] | None
) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if any label_id is not in the registry."""
    valid_label_ids = _registry_id_values(ws_labels, "label_id")
    # An empty string inside a non-empty list is NOT the clear sentinel
    # (that's the empty list itself) — treat it as an unknown ID so it can't
    # ride along into the registry write.
    unknown = [
        label_id
        for label_id in labels or []
        if not label_id or label_id not in valid_label_ids
    ]
    if unknown:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Unknown label_id(s): {unknown}. These do not exist in "
                "the label registry.",
                context={"labels": labels, "unknown_labels": unknown},
                suggestions=[
                    "Use ha_config_get_label() to list valid label IDs.",
                    "Use ha_config_set_label() to create a new label.",
                    f"Available label_ids: {sorted(valid_label_ids)}",
                ],
            )
        )


def _raise_if_unknown_area(areas: list[dict[str, Any]], area_id: str) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if area_id is not in the registry."""
    valid_area_ids = _registry_id_values(areas, "area_id")
    if area_id not in valid_area_ids:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"area_id={area_id!r} does not exist in the area registry.",
                context={"area_id": area_id},
                suggestions=[
                    "Use ha_list_floors_areas() to list valid area IDs.",
                    'Pass area_id="" to clear the area assignment.',
                    f"Available area_ids: {sorted(valid_area_ids)}",
                ],
            )
        )


def _raise_if_unknown_category(
    ws_categories: list[dict[str, Any]], category: str, scope: str
) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if category is not in the scope's registry."""
    valid_category_ids = _registry_id_values(ws_categories, "category_id")
    if category not in valid_category_ids:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"category={category!r} does not exist in the {scope} "
                "category registry.",
                context={"category": category, "scope": scope},
                suggestions=[
                    f"Use ha_config_get_category(scope='{scope}') to list valid category IDs.",
                    "Use ha_config_set_category() to create a new category.",
                    f"Available category_ids: {sorted(valid_category_ids)}",
                ],
            )
        )


def _raise_if_unknown_floor(floors: list[dict[str, Any]], floor_id: str) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if floor_id is not in the registry."""
    valid_floor_ids = _registry_id_values(floors, "floor_id")
    if floor_id not in valid_floor_ids:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"floor_id={floor_id!r} does not exist in the floor registry.",
                context={"floor_id": floor_id},
                suggestions=[
                    "Use ha_list_floors_areas() to list valid floor IDs.",
                    'Pass floor_id="" to clear the floor assignment.',
                    f"Available floor_ids: {sorted(valid_floor_ids)}",
                ],
            )
        )
