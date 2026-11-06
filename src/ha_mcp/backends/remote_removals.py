"""Finite helper/entry removal ports using the existing remote client."""

from typing import Any

from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

REMOVAL_OPERATIONS = frozenset(
    {
        "helpers.remove.entity",
        "helpers.remove.storage_target",
        "helpers.remove.registry_target",
        "helpers.storage.remove",
        "config.entries.remove",
        "config.subentries.remove",
    }
)


async def execute_removal_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation == "helpers.remove.entity":
        from ha_mcp.tools.flow_helper_lookup import _read_registry_entry

        warnings: list[str] = []
        entry, reason = await _read_registry_entry(
            client, arguments["entity_id"], warnings
        )
        return {"entry": entry, "reason": reason, "warnings": warnings}
    if operation == "helpers.remove.storage_target":
        from ha_mcp.tools.component_helper_collections import stored_helper_target

        return await stored_helper_target(
            client, arguments["helper_type"], arguments["target"]
        )
    if operation == "helpers.remove.registry_target":
        from ha_mcp.tools.tools_integrations import IntegrationTools

        unique_id, result, component_used = await IntegrationTools(
            client
        )._resolve_helper_unique_id(arguments["entity_id"])
        return {
            "unique_id": unique_id,
            "registry_result": result,
            "component_used": component_used,
        }
    if operation == "helpers.storage.remove":
        from .remote_helper_ops import _check_expected

        if arguments["helper_type"] not in SIMPLE_HELPER_TYPES:
            raise UnsupportedOperation(f"helpers.storage.{arguments['helper_type']}")
        await _check_expected(client, arguments)
        helper_type = arguments["helper_type"]
        return await client.send_websocket_message(
            {"type": f"{helper_type}/delete", f"{helper_type}_id": arguments["item_id"]}
        )
    if operation in {"config.entries.remove", "config.subentries.remove"}:
        entry_id = arguments["entry_id"]
        current = await client.get_config_entry(entry_id)
        if not json_values_equal(current, arguments["expected_entry"]):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The integration changed after its removal snapshot.",
                status="not_started",
            )
        if operation == "config.entries.remove":
            return await client.delete_config_entry(entry_id)
        return await client.delete_config_subentry(entry_id, arguments["subentry_id"])
    raise UnsupportedOperation(operation)
