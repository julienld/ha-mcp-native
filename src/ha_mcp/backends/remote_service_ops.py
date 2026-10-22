"""Remote service ports reuse the current clients and completion helpers."""

from __future__ import annotations

import json
from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

SERVICE_OPERATIONS = frozenset(
    {"services.policy", "services.call_observe", "services.websocket_command"}
)


async def execute_service_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.tools.backup_access import guard_snapshot_route
    from ha_mcp.tools.tools_service import ServiceTools

    tools = ServiceTools(client, None)
    try:
        if operation == "services.policy":
            guard_snapshot_route(**arguments)
            return None
        if operation == "services.websocket_command":
            return await tools._call_ws_command(**arguments)
        if operation == "services.call_observe":
            return await _call_and_observe(tools, arguments)
    except ToolError as exc:
        payload = json.loads(str(exc))
        raise ToolFailure(
            payload["error"]["code"], payload["error"]["message"], data=payload
        ) from exc
    except Exception as exc:
        from ha_mcp.tools.helpers import exception_to_structured_error

        payload = exception_to_structured_error(
            exc,
            context={
                "domain": arguments.get("domain"),
                "service": arguments.get("service"),
                "entity_id": ",".join(arguments.get("entity_ids", [])) or None,
            },
            raise_error=False,
        )
        raise ToolFailure(
            payload["error"]["code"], payload["error"]["message"], data=payload
        ) from exc
    raise UnsupportedOperation(operation)


async def _call_and_observe(tools: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from ha_mcp.tools.tools_service import _AmbiguousDispatch

    if arguments.get("wait") and not arguments.get("include_changed_states"):
        component = await tools._call_service_via_component(
            **{
                name: arguments[name]
                for name in (
                    "domain",
                    "service",
                    "service_data",
                    "entity_ids",
                    "wait",
                    "timeout",
                    "return_response",
                )
            }
        )
        if isinstance(component, _AmbiguousDispatch):
            return {"unknown_outcome": True}
        if component is not None:
            result: dict[str, Any] = component
            return result
    return await _call_legacy(tools, arguments)


async def _call_legacy(tools: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """One first REST fire after a proven component miss; no retry after dispatch."""
    import httpx

    from ha_mcp.client.rest_client import HomeAssistantConnectionError

    entity_id = ",".join(arguments["entity_ids"]) or None
    initial = (
        await tools._validate_entity_before_wait(entity_id)
        if arguments["wait"]
        else None
    )
    try:
        raw = await tools._client.call_service(
            arguments["domain"],
            arguments["service"],
            arguments["service_data"],
            return_response=arguments["return_response"],
        )
    except HomeAssistantConnectionError as exc:
        if isinstance(exc.__cause__, httpx.TimeoutException):
            return {"unknown_outcome": True}
        raise
    changed, response, has_response, warnings = tools._split_return_response_envelope(
        raw, return_response=arguments["return_response"]
    )
    verification: dict[str, Any] = {}
    if arguments["wait"] and entity_id is not None:
        await tools._verify_state_change(
            entity_id, arguments["service"], initial, verification
        )
    result = {
        "dispatched": True,
        "confirmed": "verified_state" in verification,
        "partial": False,
        "changed_states": changed,
        "transitions": [
            {
                "entity_id": entity_id,
                "old_state": {"state": initial},
                "new_state": {"state": verification["verified_state"]},
            }
        ]
        if "verified_state" in verification
        else [],
        "warnings": [*warnings, *verification.get("warnings", [])],
    }
    if has_response:
        result["service_response"] = response
    return result
