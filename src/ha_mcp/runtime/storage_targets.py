"""Stable storage IDs and renamed registry entities share one target policy."""

from typing import Any


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
