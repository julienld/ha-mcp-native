"""Existing remote transports behind finite integration-inspection ports."""

from typing import Any

from ha_mcp.tool_api import UnsupportedOperation

INSPECTION_OPERATIONS = frozenset(
    {
        "integrations.entries",
        "integrations.log_levels",
        "integrations.knx_project",
        "integrations.diagnostics",
    }
)


async def execute_inspection_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation == "integrations.entries":
        from ha_mcp.tools.tools_integrations import _fetch_entries_via_component

        rows = await _fetch_entries_via_component(client, **arguments)
        return {"entries": rows} if rows is not None else None
    if operation == "integrations.log_levels":
        return await client.send_websocket_message({"type": "logger/log_info"})
    if operation == "integrations.knx_project":
        return await client.send_websocket_message({"type": "knx/get_knx_project"})
    if operation == "integrations.diagnostics":
        endpoint = f"/diagnostics/config_entry/{arguments['entry_id']}"
        if arguments.get("device_id"):
            endpoint += f"/device/{arguments['device_id']}"
        return await client._request("GET", endpoint, timeout=arguments["timeout"])
    raise UnsupportedOperation(operation)
