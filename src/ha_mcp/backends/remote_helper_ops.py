"""Bounded helper ports using existing remote collection/flow clients."""

from __future__ import annotations

import json
from typing import Any

from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

HELPER_OPERATIONS = frozenset(
    {"helpers.storage.schemas", "helpers.storage.read", "helpers.storage.write"}
)


async def execute_helper_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    from ha_mcp._vendor.fastmcp.exceptions import ToolError

    try:
        return await _execute(client, operation, arguments)
    except ToolError as exc:
        payload = json.loads(str(exc))
        raise ToolFailure(
            payload["error"]["code"], payload["error"]["message"], data=payload
        ) from exc


async def _execute(
    client: Any, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from ha_mcp.tools.component_helper_collections import (
        fetch_helper_schemas,
        read_helper_item,
        write_helper_item,
    )

    if operation == "helpers.storage.schemas":
        return {"types": await fetch_helper_schemas(client) or {}}
    helper_type = arguments["helper_type"]
    if helper_type not in SIMPLE_HELPER_TYPES:
        raise UnsupportedOperation(f"helpers.storage.{helper_type}")
    if operation == "helpers.storage.read":
        result = await read_helper_item(client, **arguments)
        return result if result is not None else await _legacy_read(client, arguments)
    if operation == "helpers.storage.write":
        result = await write_helper_item(client, **arguments)
        return result if result is not None else await _legacy_write(client, arguments)
    raise UnsupportedOperation(operation)


async def _legacy_read(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.tools.component_helper_collections import registry_entity_id
    from ha_mcp.tools.config_helpers.listing import listed_items

    helper_type = arguments["helper_type"]
    item_id, entity_id = arguments.get("item_id"), arguments.get("entity_id")
    if item_id is None and entity_id is not None:
        entry = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        row = entry.get("result") or {}
        if row.get("platform") == helper_type:
            item_id = row.get("unique_id")
    listed = await client.send_websocket_message({"type": f"{helper_type}/list"})
    if not listed.get("success"):
        raise ToolFailure(
            "SERVICE_CALL_FAILED", f"Could not read {helper_type} storage collection."
        )
    item = next(
        (
            item
            for item in listed_items(listed.get("result"))
            if item.get("id") == item_id
        ),
        None,
    )
    if item is None:
        return {
            "success": False,
            "error": {
                "code": "not_found",
                "message": f"No {helper_type} item {item_id!r}",
            },
        }
    return {
        "success": True,
        "item_id": item_id,
        "item": item,
        "entity_id": entity_id
        or await registry_entity_id(client, helper_type, item_id),
    }


async def _legacy_write(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.tools.component_helper_collections import registry_entity_id
    from ha_mcp.tools.config_helpers.update import _apply_update_registry_and_category

    helper_type, action = arguments["helper_type"], arguments["action"]
    if action not in {"create", "update"}:
        raise UnsupportedOperation(f"helpers.storage.{action}")
    payload = dict(arguments["data"])
    if {"type", "id", f"{helper_type}_id"}.intersection(payload):
        raise ToolFailure(
            "INVALID_ARGUMENTS",
            "Protected helper request envelope field.",
            status="not_started",
        )
    if action == "update":
        payload[f"{helper_type}_id"] = arguments["item_id"]
    raw = await client.send_websocket_message(
        {"type": f"{helper_type}/{action}", **payload}
    )
    if raw.get("success") is not True:
        # The primitive returned an error after being sent; never replay it.
        raise ToolFailure(
            "SERVICE_CALL_FAILED", str(raw.get("error", "Helper write failed"))
        )
    item = dict(raw.get("result") or {})
    entity_id = await registry_entity_id(
        client, helper_type, item.get("id", arguments.get("item_id"))
    )
    warnings: list[str] = []
    registry = arguments.get("registry") or {}
    applied: dict[str, Any] = {}
    if entity_id and registry:
        await _apply_update_registry_and_category(
            client,
            entity_id,
            registry.get("icon"),
            registry.get("area_id"),
            registry.get("labels"),
            registry.get("category"),
            applied,
            warnings,
        )
    return {
        "success": True,
        "item": item,
        "entity_id": entity_id,
        "registry_applied": applied,
        "warnings": warnings,
    }
