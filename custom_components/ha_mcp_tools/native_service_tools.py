"""Describe the bounded Core command map without claiming arbitrary transport."""

from copy import deepcopy
from dataclasses import replace

from ha_mcp.tool_api import ToolDefinition

from .native_raw_commands import RAW_COMMANDS


def service_definition(definition: ToolDefinition) -> ToolDefinition:
    schema = deepcopy(definition.input_schema)
    schema["properties"]["ws_command"]["description"] = (
        "Native mode maps these known one-shot commands to Core owners: "
        + ", ".join(sorted(RAW_COMMANDS))
        + ". Other commands are refused before dispatch. Use a dedicated tool "
        "or the legacy runtime for unported commands."
    )
    schema["properties"]["data"]["description"] = (
        "Extra service-call parameters beyond entity_id (e.g. {'temperature': 22}). "
        "If entity_id is also present in data, the entity_id parameter wins. "
        "For ws_command, data is the command payload without type or id."
    )
    schema["properties"]["wait"]["description"] = (
        "If True (default), confirm state-changing services for each target in "
        "entity_id, including comma-separated targets. After service execution, allow "
        "up to 10 seconds for pending confirmations. Other calls do not wait for state confirmation. "
        "An already matching target can confirm immediately; an expired confirmation "
        "is partial and never retries the service."
    )
    return replace(
        definition,
        description=(
            "Call a Home Assistant service and report its observed result.\n\n"
            "Prefer a dedicated tool when it covers the operation. For other services, "
            "use domain and service (e.g. light.turn_on); ha_list_services lists their fields. "
            "Pass targets in entity_id and extra parameters in data. Results are compact "
            "by default; use verbose for full details. For service guidance, use ha_get_skill_guide.\n\n"
            "ws_command maps a bounded set of known one-shot commands directly "
            "to Core owners. Its parameter description lists the supported names; "
            "other commands are refused before dispatch. Keep the legacy runtime "
            "for unported commands.\n\n"
            "Unavailable in Read Only Mode. Use dedicated read tools there."
        ),
        input_schema=schema,
        meta={
            **definition.meta,
            "ha_mcp": {
                **definition.meta.get("ha_mcp", {}),
                "native_support": {
                    "partial": True,
                    "note": "Core service calls and the listed bounded command mappings are supported; arbitrary commands are unavailable.",
                    "commands": sorted(RAW_COMMANDS),
                },
            },
        },
    )
