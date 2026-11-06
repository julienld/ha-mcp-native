"""Zone operation adapters reuse the standalone client's existing primitives."""

from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

ZONE_OPERATIONS = frozenset(
    {"zones.target.snapshot", "zones.storage.delete", "entities.removal.wait"}
)


async def execute_zone_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    from ha_mcp.tools.tools_zones import ZoneTools

    tools = ZoneTools(client)
    if operation == "zones.target.snapshot":
        return await tools._zone_target_snapshot(arguments["zone_id"])
    if operation == "zones.storage.delete":
        from .remote_helper_ops import _check_expected

        await _check_expected(client, {**arguments, "helper_type": "zone"})
        return await client.send_websocket_message(
            {"type": "zone/delete", "zone_id": arguments["item_id"]}
        )
    if operation == "entities.removal.wait":
        from ha_mcp.errors import HomeAssistantAuthError, HomeAssistantConnectionError
        from ha_mcp.tools.ws_waiters import wait_for_entity_removed

        try:
            return await wait_for_entity_removed(client, arguments["entity_id"])
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as error:
            raise ToolFailure("VERIFICATION_FAILED", str(error)) from error
    raise UnsupportedOperation(operation)
