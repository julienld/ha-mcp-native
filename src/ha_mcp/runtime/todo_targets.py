"""Resolve the same first matching todo item as Core, with a stable selector."""

import copy
from typing import Any

from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_hash import json_values_equal


def todo_target(items: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    """Core matches UID or summary in list order; neither has global priority."""
    current = first_matching_item(items, selector)
    if current is None:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "Todo item not found.", status="not_started"
        )
    pinned = current.get("uid") or selector
    if not json_values_equal(first_matching_item(items, pinned), current):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The todo UID also selects another item's summary; use an unambiguous item.",
            status="not_started",
        )
    return {"selector": pinned, "current_item": copy.deepcopy(current)}


def first_matching_item(
    items: list[dict[str, Any]], selector: str
) -> dict[str, Any] | None:
    return next(
        (item for item in items if selector in (item.get("uid"), item.get("summary"))),
        None,
    )


def check_todo_target(
    items: list[dict[str, Any]], selector: str, expected: dict[str, Any]
) -> None:
    if not json_values_equal(first_matching_item(items, selector), expected):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The todo item changed after it was read; no mutation was sent.",
            status="not_started",
        )
