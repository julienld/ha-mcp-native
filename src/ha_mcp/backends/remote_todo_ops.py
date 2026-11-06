"""Explicit todo operations over the established remote clients."""

from typing import Any

from ha_mcp.runtime.todo_targets import check_todo_target, todo_target
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

TODO_OPERATIONS = frozenset(
    {
        "todo.states",
        "todo.move",
        "todo.service",
        "todo.bridge",
        "todo.items",
        "todo.item.prepare",
        "todo.item.add",
        "todo.item.update",
        "todo.item.remove",
        "todo.backup.capture",
    }
)


async def execute_todo_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    from ha_mcp.client.rest_client import (
        HomeAssistantCommandNotSent,
        HomeAssistantConnectionError,
    )
    from ha_mcp.runtime.error_classification import classified_error

    try:
        return await _execute_todo_operation(client, operation, arguments)
    except HomeAssistantConnectionError as error:
        data = classified_error(error)
        raise ToolFailure(
            data["error"]["code"],
            data["error"]["message"],
            data=data,
            status="not_started"
            if isinstance(error, HomeAssistantCommandNotSent)
            else "outcome_unknown",
        ) from error


async def _execute_todo_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation == "todo.bridge":
        return await _execute_todo_bridge(client, arguments)
    command = {
        "todo.move": {"type": "todo/item/move"},
        "todo.service": {"type": "call_service", "domain": "todo"},
    }.get(operation)
    if command is not None:
        return await client.send_websocket_message({**command, **arguments})
    if operation == "todo.states":
        return await client.get_states()
    if operation == "todo.items":
        return await client.send_websocket_message(
            {"type": "todo/item/list", "entity_id": arguments["entity_id"]}
        )
    if operation == "todo.backup.capture":
        return await _capture_backup(client, arguments)
    if operation == "todo.item.prepare":
        items = await _items(client, arguments["entity_id"])
        return todo_target(items, arguments["item"])
    if operation in {"todo.item.add", "todo.item.update", "todo.item.remove"}:
        if operation != "todo.item.add":
            data = arguments["data"]
            check_todo_target(
                await _items(client, data["entity_id"]),
                data["item"],
                arguments["expected_item"],
            )
        from ha_mcp.runtime.errors import ws_failure_code

        data = dict(arguments["data"])
        entity_id = data.pop("entity_id")
        reply = await client.send_websocket_message(
            {
                "type": "call_service",
                "domain": "todo",
                "service": operation.rsplit(".", 1)[1] + "_item",
                "target": {"entity_id": entity_id},
                "service_data": data,
            }
        )
        if not reply.get("success"):
            raise ToolFailure(ws_failure_code(reply).value, str(reply.get("error")))
        return reply.get("result")
    raise UnsupportedOperation(operation)


async def _execute_todo_bridge(client: Any, arguments: dict[str, Any]) -> Any:
    import json

    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.tools.component_todo import todo_call

    arguments = dict(arguments)
    expected = arguments.pop("expected_item", None)
    expected_items = arguments.pop("expected_items", None)
    entity_id = arguments["entity_id"]
    if expected is not None:
        _bridge_selector(await _items(client, entity_id), arguments["uid"], expected)
    if expected_items is not None:
        items = await _items(client, entity_id)
        arguments["refs"] = [
            _bridge_selector(items, ref, row)
            for ref, row in zip(arguments["refs"], expected_items, strict=True)
        ]
    try:
        return await todo_call(client, **arguments)
    except ToolError as exc:
        data = json.loads(str(exc))
        raise ToolFailure(
            data["error"]["code"], data["error"]["message"], data=data
        ) from exc


def _bridge_selector(
    items: list[dict[str, Any]], ref: str, expected: dict[str, Any]
) -> str:
    from ha_mcp.runtime.todo_values import find_item
    from ha_mcp.utils.config_hash import json_values_equal

    selector = expected.get("uid") or ref
    stored = next((item for item in items if item.get("uid") == selector), None)
    if (
        stored is not None
        and json_values_equal(stored, expected)
        and find_item(items, selector) is not stored
    ):
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            f"Another item is named {selector!r}, so Core would change that one; nothing was changed",
            status="not_started",
        )
    check_todo_target(items, selector, expected)
    return str(selector)


async def _items(client: Any, entity_id: str) -> list[dict[str, Any]]:
    result = await client.send_websocket_message(
        {"type": "todo/item/list", "entity_id": entity_id}
    )
    if not result.get("success"):
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "Todo list unavailable.", status="not_started"
        )
    items: list[dict[str, Any]] = result["result"]["items"]
    return items


async def _capture_backup(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.backup_manager import (
        _CAPTURE_TRANSIENT_ERRORS,
        MandatoryBackupError,
        get_backup_manager,
    )
    from ha_mcp.config import get_global_settings

    settings = get_global_settings()
    if not settings.enable_auto_backup:
        return {}
    source = arguments["source_tool"]
    if source not in {"ha_set_todo_item", "ha_remove_todo_item"}:
        raise ValueError("Unsupported todo snapshot source")
    key = f"{arguments['entity_id']}::{arguments['item']}"
    try:
        manager = get_backup_manager(client, settings)
        await manager.maybe_snapshot_known(
            "todo_item",
            key,
            {"todo_entity_id": arguments["entity_id"], **arguments["current_item"]},
            tool_name=source,
            mandatory=True,
        )
    except MandatoryBackupError as error:
        return {
            "warnings": [f"No pre-write backup of {key} was taken: {error.safe_cause}"]
        }
    except _CAPTURE_TRANSIENT_ERRORS as error:
        return {
            "warnings": [
                f"No pre-write backup of {key} was taken: {type(error).__name__}"
            ]
        }
    return {}
