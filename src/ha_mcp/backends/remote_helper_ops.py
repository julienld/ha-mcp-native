"""Bounded helper ports using existing remote collection/flow clients."""

from __future__ import annotations

import json
from typing import Any

from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

HELPER_OPERATIONS = frozenset(
    {
        "helpers.storage.schemas",
        "helpers.storage.read",
        "helpers.storage.write",
        "helpers.storage.list",
        "helpers.registry.snapshot",
        "helpers.backup.capture",
        "helpers.flow.entries",
        "helpers.entities.for_entry",
        "helpers.entities.update",
    }
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
    )

    if operation == "helpers.storage.schemas":
        return {"types": await fetch_helper_schemas(client) or {}}
    if operation == "helpers.registry.snapshot":
        return await _registry_snapshot(client, arguments)
    if operation in {
        "helpers.flow.entries",
        "helpers.entities.for_entry",
        "helpers.entities.update",
    }:
        return await _flow_records(client, operation, arguments)
    if operation == "helpers.backup.capture":
        return await _capture_backup(client, arguments)
    helper_type = arguments["helper_type"]
    if helper_type not in SIMPLE_HELPER_TYPES:
        raise UnsupportedOperation(f"helpers.storage.{helper_type}")
    if operation == "helpers.storage.list":
        from ha_mcp.tools.config_helpers.listing import listed_items

        raw = await client.send_websocket_message({"type": f"{helper_type}/list"})
        if raw.get("success") is not True:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                f"Could not list {helper_type} storage collection.",
            )
        return {"items": listed_items(raw.get("result"))}
    if operation == "helpers.storage.read":
        result = await read_helper_item(client, **arguments)
        return result if result is not None else await _legacy_read(client, arguments)
    if operation == "helpers.storage.write":
        return await _write_storage(client, arguments)
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


async def _legacy_write(
    client: Any, arguments: dict[str, Any], *, wait: bool = False
) -> dict[str, Any]:
    from ha_mcp.tools.component_helper_collections import registry_entity_id
    from ha_mcp.tools.config_helpers.update import _apply_update_registry_and_category

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
    if entity_id and wait and helper_type != "tag":
        from ha_mcp.tools.ws_waiters import wait_for_entity_registered

        if not await wait_for_entity_registered(client, entity_id):
            warnings.append(f"Helper was saved but {entity_id} is not yet queryable.")
    return {
        "success": True,
        "item": item,
        "entity_id": entity_id,
        "registry_applied": applied,
        "warnings": warnings,
    }


async def _registry_snapshot(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in arguments["registries"]:
        if kind not in {"area", "floor", "label", "category"}:
            raise UnsupportedOperation(f"registries.{kind}")
        scopes = arguments.get("category_scopes", []) if kind == "category" else [None]
        for scope in scopes:
            request = {"type": f"config/{kind}_registry/list"}
            if scope is not None:
                request["scope"] = scope
            raw = await client.send_websocket_message(request)
            if raw.get("success") is not True:
                raise ToolFailure(
                    "CONNECTION_FAILED",
                    f"Could not read {kind} registry for validation.",
                )
            if kind == "category":
                result.setdefault("categories", {})[scope] = raw["result"]
            else:
                result[f"{kind}s"] = raw["result"]
    return result


async def _capture_backup(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.config import get_global_settings
    from ha_mcp.runtime.helper_tools import HelperConfigTools
    from ha_mcp.tools.auto_backup import _capture_pre_write_snapshot

    settings = get_global_settings()
    if not settings.enable_auto_backup:
        return {}
    domain = arguments.get("backup_domain", f"helper_{arguments['helper_type']}")
    source = arguments.get("source_tool", "ha_config_set_helper")
    function = HelperConfigTools.ha_config_set_helper
    id_param = "helper_id"
    if (domain, source) != (f"helper_{arguments['helper_type']}", function.__name__):
        if (domain, source, arguments["helper_type"]) not in {
            ("zone", "ha_set_zone", "zone"),
            ("zone", "ha_remove_zone", "zone"),
        }:
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "Unsupported backup identity.",
                status="not_started",
            )
        from ha_mcp.runtime.zone_writes import ZoneWriteTools

        function = getattr(ZoneWriteTools, source)
        id_param = "zone_id"
    warning = await _capture_pre_write_snapshot(
        function,
        (),
        {id_param: arguments["backup_id"]},
        settings=settings,
        explicit_client=client,
        domain=domain,
        domain_fn=None,
        id_param=id_param,
        id_fn=None,
        mandatory=False,
        domain_resolver=None,
    )
    return {"warnings": [warning]} if warning else {}


async def _flow_records(
    client: Any, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    from ha_mcp.tools.config_helpers.registry import (
        _apply_registry_updates_to_entity,
        _get_entities_for_config_entry,
    )

    warnings: list[str] = []
    if operation == "helpers.entities.for_entry":
        entities = await _get_entities_for_config_entry(
            client, arguments["config_entry_id"], warnings
        )
        return {"entities": entities, "warnings": warnings}
    if operation == "helpers.entities.update":
        registry = arguments["registry"]
        applied = [
            await _apply_registry_updates_to_entity(
                client,
                entity_id,
                registry.get("area_id"),
                registry.get("labels"),
                registry.get("category"),
                registry.get("icon"),
                warnings,
            )
            for entity_id in arguments["entity_ids"]
        ]
        return {"applied": applied, "warnings": warnings}
    raw = await client.send_websocket_message(
        {"type": "config_entries/get", "domain": arguments["helper_type"]}
    )
    if raw.get("success") is not True:
        raise ToolFailure(
            "CONNECTION_FAILED",
            "Could not list helper entries for collision detection.",
        )
    return {
        "items": [
            {"id": entry["entry_id"], "name": entry["title"]} for entry in raw["result"]
        ]
    }


async def _write_storage(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.tools.component_helper_collections import write_helper_item

    payload = dict(arguments)
    payload.pop("expected_item", None)
    wait = payload.pop("wait_for_entity", False)
    await _check_expected(client, arguments)
    result = await write_helper_item(client, **payload)
    return (
        result
        if result is not None
        else await _legacy_write(client, payload, wait=wait)
    )


async def _check_expected(client: Any, arguments: dict[str, Any]) -> None:
    """Same optimistic readback check for update and delete operations."""
    expected = arguments.get("expected_item")
    if expected is not None:
        current = await _execute(
            client,
            "helpers.storage.read",
            {"helper_type": arguments["helper_type"], "item_id": arguments["item_id"]},
        )
        if current.get("item") != expected:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The helper changed after its snapshot was read; no update was sent.",
                status="not_started",
            )
