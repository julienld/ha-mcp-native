"""Finite config support operations over the existing standalone clients."""

import asyncio
from typing import Any

from ha_mcp.tool_api import UnsupportedOperation

CONFIG_FINALIZE_OPERATIONS = frozenset(
    {
        "blueprints.render",
        "config.references.snapshot",
        "config.entity_id.after_write",
        "entities.registration.wait",
        "entities.category.write",
        "config.transform",
        "entities.state.wait",
        "config.automation.allocate_id",
        "config.automation.runtime_target",
    }
)


async def execute_config_finalize(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation in {
        "config.automation.allocate_id",
        "config.automation.runtime_target",
        "entities.state.wait",
    }:
        return await _automation_operation(client, operation, arguments)
    if operation == "config.transform":
        from ha_mcp.utils.python_sandbox import safe_execute

        return await asyncio.to_thread(
            safe_execute, arguments["expression"], arguments["config"]
        )
    if operation == "blueprints.render":
        return await client.send_websocket_message(
            {"type": "blueprint/substitute", **arguments}
        )
    if operation == "config.references.snapshot":
        from ha_mcp.tools.component_config_reads import (
            fetch_reference_data_via_component,
        )

        result = await fetch_reference_data_via_component(client)
        if result is not None:
            return result
        services, states = await asyncio.gather(
            client.get_services(), client.get_states()
        )
        return {
            "services": services,
            "entity_ids": [
                row["entity_id"]
                for row in states
                if isinstance(row, dict) and isinstance(row.get("entity_id"), str)
            ],
        }
    if operation == "config.entity_id.after_write":
        if arguments["domain"] == "automation":
            from ha_mcp.tools.ws_waiters import wait_for_automation_entity_by_unique_id

            return await wait_for_automation_entity_by_unique_id(
                client, arguments["storage_key"]
            )
        from ha_mcp.tools.entity_registration import resolve_entity_id_after_write

        return await resolve_entity_id_after_write(client, **arguments)
    if operation == "entities.registration.wait":
        from ha_mcp.tools.ws_waiters import wait_for_entity_registered

        return await wait_for_entity_registered(client, arguments["entity_id"])
    if operation == "entities.category.write":
        from ha_mcp.tools.config_write_helpers import apply_entity_category

        result = {}
        await apply_entity_category(
            client,
            arguments["entity_id"],
            arguments["category"],
            arguments["scope"],
            result,
            arguments["entity_type"],
        )
        return result
    raise UnsupportedOperation(operation)


async def _automation_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation == "config.automation.allocate_id":
        import time

        return str(int(time.time() * 1000))
    if operation == "config.automation.runtime_target":
        from ha_mcp.tools.tools_config_automations import AutomationConfigTools

        return await AutomationConfigTools(client)._resolve_automation_entity_id_strict(
            arguments["identifier"]
        )
    if operation == "entities.state.wait":
        from ha_mcp.tools.ws_waiters import wait_for_state_change

        return await wait_for_state_change(
            client, arguments["entity_id"], expected_state=arguments["expected_state"]
        )
    raise UnsupportedOperation(operation)
