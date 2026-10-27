"""Shared raw-command admission; native mode still has no raw transport."""

from typing import Any

from ha_mcp.errors import create_validation_error

from .action_params import _parse_json_dict_param
from .errors import raise_failure

BLOCKED_WS_WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "config/core/update",
        "lovelace/config/save",
        "ha_mcp_tools/dashboard_edit",
        "lovelace/dashboards/create",
        "lovelace/dashboards/delete",
        "lovelace/dashboards/update",
        # Resource writes carry the same wrapping-tool validation as the
        # dashboard commands above -- auto-backup, the #1072 HA-config-YAML
        # misroute rejection, the inline size cap, and the data:-URL routing
        # guard all live in ha_config_set_dashboard_resource (#2060).
        "lovelace/resources/create",
        "lovelace/resources/delete",
        "lovelace/resources/update",
        "config/area_registry/delete",
        "config/area_registry/disable",
        "config/area_registry/update",
        "config/device_registry/delete",
        "config/device_registry/disable",
        "config/device_registry/update",
        "config/device_registry/remove",
        "config/device_registry/remove_config_entry",
        "config/entity_registry/delete",
        "config/entity_registry/disable",
        "config/entity_registry/update",
        "config/entity_registry/remove",
        "config/floor_registry/create",
        "config/floor_registry/delete",
        "config/floor_registry/update",
        "config/label_registry/create",
        "config/label_registry/delete",
        "config/label_registry/update",
        "config/category_registry/create",
        "config/category_registry/delete",
        "config/category_registry/update",
    }
)

_WS_COMMAND_EVENT_BLOCKLIST = frozenset(
    {
        "render_template",  # event-based; use ha_eval_template instead
        "system_health/info",  # two-phase (see tools_system._fetch_health_info)
        "assist_pipeline/run",  # streams pipeline events
    }
)

_WS_STREAMING_SUBSTRINGS = ("subscribe", "stream", "start_preview")

_WS_COMMAND_SERVICE_INVOKERS = frozenset({"call_service", "execute_script"})

_WS_RESERVED_ENVELOPE_KEYS = frozenset({"type", "id"})


def _is_streaming_ws_command(command_type: str) -> bool:
    """Return True for subscription / streaming / two-phase WS commands."""
    lowered = command_type.lower()
    if lowered in _WS_COMMAND_EVENT_BLOCKLIST:
        return True
    return any(sub in lowered for sub in _WS_STREAMING_SUBSTRINGS)


def validate_ws_request(
    ws_command: str, data: Any, *, domain: str | None, service: str | None
) -> tuple[str, dict[str, Any]]:
    command_type = ws_command.strip()
    if domain is not None or service is not None:
        raise_failure(
            create_validation_error(
                "Provide either domain + service (a registered service call) "
                "OR ws_command (a raw WebSocket command), not both.",
                parameter="ws_command",
            )
        )
    if not command_type:
        raise_failure(
            create_validation_error(
                "ws_command must be a non-empty WebSocket command type "
                "(the message's 'type' field).",
                parameter="ws_command",
            )
        )
    if _is_streaming_ws_command(command_type):
        raise_failure(
            create_validation_error(
                f"ws_command '{command_type}' is a streaming or two-phase "
                "command; ha_call_service only sends one-shot "
                "request/response commands. For template rendering use "
                "ha_eval_template.",
                parameter="ws_command",
            )
        )
    if command_type.lower() in _WS_COMMAND_SERVICE_INVOKERS:
        raise_failure(
            create_validation_error(
                f"ws_command '{command_type}' invokes Home Assistant services "
                "and would bypass ha_call_service's safeguards. Use the "
                "domain/service parameters for service calls instead.",
                parameter="ws_command",
            )
        )
    if command_type.lower().startswith("ha_mcp_tools/"):
        raise_failure(
            create_validation_error(
                "ha_call_service cannot invoke 'ha_mcp_tools/*' WebSocket "
                "commands. Use the dedicated ha-mcp tools instead.",
                parameter="ws_command",
            )
        )
    if command_type.lower() in BLOCKED_WS_WRITE_COMMANDS:
        raise_failure(
            create_validation_error(
                f"ws_command '{command_type}' mutates persistent state that a "
                "dedicated tool guards with backups and conflict checks. Use "
                "the corresponding ha-mcp tool (e.g. ha_config_set_dashboard, "
                "ha_set_area_or_floor, ha_remove_entity) instead.",
                parameter="ws_command",
            )
        )
    command_params = (
        _parse_json_dict_param(
            data, type_error_message="ws_command data must be a JSON object"
        )
        or {}
    )
    reserved = _WS_RESERVED_ENVELOPE_KEYS & command_params.keys()
    if reserved:
        raise_failure(
            create_validation_error(
                "data must not contain the reserved WebSocket envelope key(s) "
                f"{', '.join(sorted(reserved))}; the command type is set by "
                "ws_command, and the message id is managed by the transport.",
                parameter="data",
            )
        )
    return command_type, command_params
