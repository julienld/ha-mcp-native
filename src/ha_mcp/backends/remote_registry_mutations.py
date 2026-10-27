"""Registry mutations reuse standalone WebSocket commands with explicit CAS."""

from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

REGISTRY_MUTATION_OPERATIONS = frozenset(
    {
        "registry.backup.capture",
        *{
            f"registry.{domain}.{action}"
            for domain in ("category", "area", "floor")
            for action in ("read", "create", "update", "delete")
        },
    }
)


async def _registry_read(
    client: Any, domain: str, data: dict[str, Any]
) -> dict[str, Any] | None:
    response = await client.send_websocket_message(
        {
            "type": f"config/{domain}_registry/list",
            **({"scope": data["scope"]} if domain == "category" else {}),
        }
    )
    if not response.get("success"):
        raise ToolFailure(
            "SERVICE_CALL_FAILED", f"Failed to read {domain}: {response.get('error')}"
        )
    return next(
        (
            row
            for row in response.get("result", [])
            if row.get(f"{domain}_id") == data[f"{domain}_id"]
        ),
        None,
    )


async def execute_registry_mutation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation == "registry.backup.capture":
        return await _capture_registry_backup(client, arguments)
    if operation not in REGISTRY_MUTATION_OPERATIONS:
        raise UnsupportedOperation(operation)
    _, domain, action = operation.split(".")
    if action == "read":
        return await _registry_read(client, domain, arguments)
    data = arguments["data"]
    if {"type", "id"}.intersection(data):
        raise UnsupportedOperation("registry.payload.envelope")
    previous = (
        await _registry_read(client, domain, data) if action != "create" else None
    )
    if not json_values_equal(previous, arguments["expected_item"]):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            f"The {domain} changed after its snapshot was read; no mutation was sent.",
            status="not_started",
        )
    return await client.send_websocket_message(
        {"type": f"config/{domain}_registry/{action}", **data}
    )


async def _capture_registry_backup(
    client: Any, arguments: dict[str, Any]
) -> dict[str, Any]:
    from ha_mcp.backup_manager import _CAPTURE_TRANSIENT_ERRORS, get_backup_manager
    from ha_mcp.config import get_global_settings

    domain = arguments["domain"]
    sources = {
        "category": {"ha_config_set_category", "ha_config_remove_category"},
        "area_or_floor": {"ha_set_area_or_floor", "ha_remove_area_or_floor"},
    }
    if domain not in sources or arguments["source_tool"] not in sources[domain]:
        raise UnsupportedOperation("registry.backup.capture.domain")
    settings = get_global_settings()
    if (
        not settings.enable_auto_backup
        or not arguments.get("backup_id")
        or arguments.get("current_config") is None
    ):
        return {}
    try:
        await get_backup_manager(client, settings).maybe_snapshot_known(
            domain,
            arguments["backup_id"],
            arguments["current_config"],
            tool_name=arguments["source_tool"],
        )
    except _CAPTURE_TRANSIENT_ERRORS as error:
        return {"warnings": [f"No pre-write backup was taken: {error}"]}
    return {}
