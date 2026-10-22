"""Finite integration-management ports on the existing remote client."""

from typing import Any

from ha_mcp.tool_api import UnsupportedOperation

from .remote_integration_reads import (
    INSPECTION_OPERATIONS,
    execute_inspection_operation,
)

INTEGRATION_OPERATIONS = frozenset(
    {
        *INSPECTION_OPERATIONS,
        "integrations.log_level.set",
        "integrations.enabled.set",
        "integrations.identity.entities",
        "integrations.identity.devices",
        "integrations.identity.unique_id",
        "integrations.identity.domain_unique_ids",
        "config.entries.list",
    }
)


async def execute_integration_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation in INSPECTION_OPERATIONS:
        return await execute_inspection_operation(client, operation, arguments)
    if operation == "integrations.identity.entities":
        from .remote_reconfigure_identity import _entry_entity_rows

        return await _entry_entity_rows(client, arguments["entry_id"])
    if operation == "integrations.identity.devices":
        from .remote_reconfigure_identity import _device_registry_rows

        return await _device_registry_rows(client, arguments["entry_id"])
    if operation == "integrations.identity.unique_id":
        from ha_mcp.tools.component_config_entries import fetch_config_entry_unique_id

        value = await fetch_config_entry_unique_id(client, arguments["entry_id"])
        return value._asdict()
    if operation == "integrations.identity.domain_unique_ids":
        from ha_mcp.tools.component_config_entries import fetch_domain_unique_ids

        return await fetch_domain_unique_ids(client, arguments["domain"])
    if operation == "config.entries.list":
        return await client.list_config_entries()
    if operation == "integrations.log_level.set":
        return await client.send_websocket_message(
            {
                "type": "logger/integration_log_level",
                "integration": arguments["domain"],
                "level": arguments["level"],
                "persistence": "once",
            }
        )
    if operation == "integrations.enabled.set":
        return await client.send_websocket_message(
            {
                "type": "config_entries/disable",
                "entry_id": arguments["entry_id"],
                "disabled_by": None if arguments["enabled"] else "user",
            }
        )
    raise UnsupportedOperation(operation)
