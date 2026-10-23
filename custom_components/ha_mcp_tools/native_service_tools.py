"""Describe the supported service path without claiming a raw transport API."""

from copy import deepcopy
from dataclasses import replace

from ha_mcp.tool_api import ToolDefinition


def service_definition(definition: ToolDefinition) -> ToolDefinition:
    schema = deepcopy(definition.input_schema)
    schema["properties"]["ws_command"]["description"] = (
        "Not supported in native mode. Retained for client compatibility; every "
        "non-null ws_command is refused before command dispatch. Use a dedicated "
        "tool, or the legacy runtime for raw WebSocket commands."
    )
    schema["properties"]["data"]["description"] = (
        "Extra service-call parameters beyond entity_id (e.g. {'temperature': 22}). "
        "If entity_id is also present in data, the entity_id parameter wins."
    )
    return replace(
        definition,
        description=(
            "Call a Home Assistant service and report its observed result.\n\n"
            "Prefer a dedicated tool when it covers the operation. For other services, "
            "use domain and service (e.g. light.turn_on); ha_list_services lists their fields. "
            "Pass targets in entity_id and extra parameters in data. Results are compact "
            "by default; use verbose for full details. For service guidance, use ha_get_skill_guide.\n\n"
            "ws_command is not supported in native mode. Use dedicated tools, "
            "or keep the legacy runtime for raw WebSocket commands.\n\n"
            "Unavailable in Read Only Mode. Use dedicated read tools there."
        ),
        input_schema=schema,
        meta={
            **definition.meta,
            "ha_mcp": {
                **definition.meta.get("ha_mcp", {}),
                "native_support": {
                    "partial": True,
                    "note": "Core service calls are supported; every ws_command is unsupported.",
                },
            },
        },
    )
