"""Compare the exact registry identity and fields covered by a recovery image."""

from typing import Any

from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_hash import json_values_equal


def check_expected_registry(hass: Any, expected: dict[str, Any] | None) -> None:
    """Synchronous admission: call after authorization, immediately before dispatch."""
    if expected is None:
        return
    from homeassistant.helpers import entity_registry as er

    if (
        not isinstance(expected, dict)
        or set(expected) != {"entity_id", "id", "platform", "unique_id", "values"}
        or not isinstance(expected["platform"], str)
        or not isinstance(expected["unique_id"], str)
        or not isinstance(expected["values"], dict)
        or set(expected["values"]) - {"icon", "name"}
    ):
        raise ToolFailure(
            "INVALID_ARGUMENTS", "Invalid registry pre-image", status="not_started"
        )
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        expected["platform"], expected["platform"], expected["unique_id"]
    )
    row = registry.async_get(entity_id) if entity_id is not None else None
    actual = {
        "entity_id": entity_id,
        "id": row.id if row else None,
        "platform": row.platform if row else expected["platform"],
        "unique_id": row.unique_id if row else expected["unique_id"],
        "values": {
            key: getattr(row, key) if row else None for key in expected["values"]
        },
    }
    if not json_values_equal(actual, expected):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The helper entity registry changed after its snapshot was read; no mutation was sent.",
            status="not_started",
        )
