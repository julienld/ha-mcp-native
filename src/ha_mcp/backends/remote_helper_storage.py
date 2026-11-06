"""Existing Core WebSocket collection primitives for the remote backend only."""

from typing import Any

from ha_mcp.errors import HomeAssistantError
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation


async def _legacy_read(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.tools.component_helper_collections import registry_entity_id
    from ha_mcp.tools.config_helpers.listing import listed_items

    helper_type = arguments["helper_type"]
    item_id, entity_id = arguments.get("item_id"), arguments.get("entity_id")
    if item_id is None and entity_id is not None:
        from ha_mcp.tools.config_helpers.update import _resolve_update_unique_id

        item_id = await _resolve_update_unique_id(
            client, helper_type, entity_id, entity_id, None
        )
    listed = await client.send_websocket_message({"type": f"{helper_type}/list"})
    if not listed.get("success"):
        raise ToolFailure(
            "SERVICE_CALL_FAILED",
            f"Failed to fetch {helper_type} config list: {listed.get('error', 'Unknown')}",
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
    reason = None
    try:
        entity_id = await registry_entity_id(client, helper_type, item_id)
    except (HomeAssistantError, OSError, TimeoutError) as error:
        entity_id = None
        reason = f"the entity registry could not be read ({error})"
    if entity_id is None and reason is None:
        reason = f"the {helper_type} has no entity in the registry"
    return {
        "success": True,
        "item_id": item_id,
        "item": item,
        "entity_id": entity_id,
        "entity_missing_reason": reason,
    }


async def _legacy_write(
    client: Any, arguments: dict[str, Any], *, wait: bool = False
) -> dict[str, Any]:
    from ha_mcp.tools.component_helper_collections import registry_entity_id

    helper_type, action = arguments["helper_type"], arguments["action"]
    if action not in {"create", "update"}:
        raise UnsupportedOperation(f"helpers.storage.{action}")
    payload = dict(arguments["data"])
    reserved = {"type", "id"} | ({f"{helper_type}_id"} if action == "update" else set())
    if reserved.intersection(payload):
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
        from ha_mcp.errors import ErrorCode, create_error_response
        from ha_mcp.runtime.errors import raise_failure
        from ha_mcp.runtime.helper_schema import _simple_helper_error_context
        from ha_mcp.tools.helpers import ws_failure_code

        code = ws_failure_code(raw)
        if action == "update" and code is ErrorCode.RESOURCE_NOT_FOUND:
            code = ErrorCode.CONFIG_NOT_FOUND
        raise_failure(
            create_error_response(
                code,
                f"Failed to {action} {helper_type} config: {raw.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type),
            )
        )
    item = dict(raw.get("result") or {})
    item_id = item.get("id", arguments.get("item_id"))
    lookup_error = None
    try:
        entity_id = await registry_entity_id(client, helper_type, item_id)
    except (HomeAssistantError, OSError, TimeoutError) as error:
        entity_id, lookup_error = None, str(error)
    warnings: list[str] = []
    registry = arguments.get("registry") or {}
    applied: dict[str, Any] = {}
    if entity_id is None:
        _missing_entity_warning(
            helper_type, action, item_id, registry, lookup_error, wait, warnings
        )
    if entity_id and registry:
        await _apply_registry(client, entity_id, action, registry, applied, warnings)
    if entity_id and wait and helper_type != "tag":
        await _wait_for_entity(client, entity_id, action, warnings)
    return {
        "success": True,
        "item": item,
        "entity_id": entity_id,
        "registry_applied": applied,
        "warnings": warnings,
    }


def _missing_entity_warning(
    helper_type: str,
    action: str,
    item_id: str | None,
    registry: dict[str, Any],
    lookup_error: str | None,
    wait: bool,
    warnings: list[str],
) -> None:
    supplied = ["area" if key == "area_id" else key for key in registry]
    not_applied = (
        f", so the {', '.join(supplied)} given were not applied" if supplied else ""
    )
    if action == "create":
        if lookup_error or supplied:
            reason = (
                f"its entity could not be read ({lookup_error})"
                if lookup_error
                else "its entity is not in the entity registry"
            )
            warnings.append(f"{helper_type} created, but {reason}{not_applied}.")
    else:
        reason = (
            f"the entity registry could not be read ({lookup_error})"
            if lookup_error
            else f"the {helper_type} has no entity in the registry"
        )
        not_waited = (
            " and the update was not waited on" if wait and helper_type != "tag" else ""
        )
        warnings.append(
            f"{helper_type} {item_id} updated, but {reason}{not_applied}{not_waited}."
        )


async def _apply_registry(
    client: Any,
    entity_id: str,
    action: str,
    registry: dict[str, Any],
    applied: dict[str, Any],
    warnings: list[str],
) -> None:
    from ha_mcp.tools.config_helpers.update import _apply_update_registry_and_category

    if action == "create":
        from ha_mcp.tools.config_helpers.create import (
            _apply_create_category,
            _apply_create_entity_registry,
        )

        await _apply_create_entity_registry(
            client,
            entity_id,
            registry.get("icon"),
            registry.get("area_id"),
            registry.get("labels"),
            applied,
            warnings,
            registry.get("icon"),
        )
        await _apply_create_category(
            client, entity_id, registry.get("category"), applied, warnings
        )
    else:
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


async def _wait_for_entity(
    client: Any,
    entity_id: str,
    action: str,
    warnings: list[str],
) -> None:
    from ha_mcp.tools.ws_waiters import wait_for_entity_registered

    try:
        if not await wait_for_entity_registered(client, entity_id):
            warnings.append(
                f"Helper created but {entity_id} not yet queryable. It may take a moment to become available."
                if action == "create"
                else f"Update applied but {entity_id} not yet queryable."
            )
    except Exception as error:  # noqa: BLE001 - the write already succeeded
        warnings.append(
            f"Helper created but verification failed: {error}"
            if action == "create"
            else f"Update applied but verification failed: {error}"
        )
