"""Stable storage IDs and renamed registry entities share one target policy."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def stored_target(
    helper_type: str, target: str, stored_ids: Any, rows: list[Any]
) -> tuple[str | None, str | None]:
    """Resolve a bare storage ID or full entity ID without a rename collision.

    A registry unique ID can refer to YAML rather than an editable stored item;
    callers must verify membership in their authoritative stored collection.
    """
    ids = {str(item_id) for item_id in stored_ids}
    own = [row for row in rows if row.get("platform") == helper_type]
    if target in ids:
        entity = next(
            (row.get("entity_id") for row in own if row.get("unique_id") == target),
            None,
        )
        return target, entity
    row = next((row for row in own if row.get("entity_id") == target), None)
    if row is None or not row.get("unique_id"):
        return None, None
    return str(row["unique_id"]), target


def raise_not_stored(
    helper_type: str, target: str, name: str | None = None
) -> NoReturn:
    """A bare id names a stored item only, never an entity by its name."""
    suggestions = [
        f"Find its id or entity_id with "
        f"ha_config_list_helpers(helper_type='{helper_type}')"
    ]
    if name:
        suggestions.append(
            f"If you meant to create a new helper named {name!r}, "
            "omit helper_id (or pass action='create')"
        )
    raise_failure(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"No stored {helper_type} has the id '{target}', and it is no "
            f"{helper_type} entity_id. An id without '{helper_type}.' is a "
            "storage id; name a helper by its entity with the full entity_id.",
            context={"helper_type": helper_type, "target": target},
            suggestions=suggestions,
        )
    )
