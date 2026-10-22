"""Zone operation adapters reuse the standalone client's existing primitives."""

from typing import Any

from ha_mcp.tool_api import UnsupportedOperation

ZONE_OPERATIONS = frozenset(
    {"zones.target.snapshot", "zones.storage.delete", "zones.removal.wait"}
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
        return await tools._remove_stored_zone(arguments["item_id"], None)
    if operation == "zones.removal.wait":
        return await tools._wait_zone_removed(arguments["entity_id"])
    raise UnsupportedOperation(operation)
