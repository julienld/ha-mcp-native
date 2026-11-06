"""
Shared utility functions for MCP tool modules.

This module provides common helper functions used across multiple tool registration modules.
"""

import logging
from typing import Any

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime.errors import websocket_error_message
from ha_mcp.runtime.health_results import (
    filter_active_repairs,
    project_repair_fields,
    summarize_theme_listing,
)
from ha_mcp.runtime.integration_logs import get_logger_levels as _shared_logger_levels
from ha_mcp.runtime.integration_logs import normalize_log_level
from ha_mcp.runtime.search_response import (
    merge_visibility_warnings,
)
from ha_mcp.runtime.websocket_policy import (
    BLOCKED_WS_WRITE_COMMANDS as BLOCKED_WS_WRITE_COMMANDS,
)
from ha_mcp.runtime.websocket_policy import (
    is_credential_ws_command as is_credential_ws_command,
)
from ha_mcp.runtime.websocket_policy import (
    normalized_path as normalized_path,
)
from ha_mcp.runtime.websocket_policy import (
    raw_ws_refusal as raw_ws_refusal,
)
from ha_mcp.runtime.websocket_policy import (
    supervisor_path as supervisor_path,
)

from ..runtime.action_constants import (
    _SERVICE_TO_STATE,
    is_single_entity_target,
)

logger = logging.getLogger(__name__)

get_logger_levels = legacy_helper_flow(_shared_logger_levels)

# Mapping from service name to the expected resulting primary state. The single
# source of truth (imported by both ``tools_service`` and ``device_control``): it
# is the confirmation HINT the server hands the component's ``call_service`` /
# ``bulk_call_service`` capability so the component confirms only on REACHING that
# state (skipping a multi-phase service's intermediate states and attribute-only
# noise) and short-circuits an idempotent no-op. ``.get(service)`` is ``None`` for
# every service without a known primary state (``set_temperature`` /
# ``set_fan_mode`` / …) — a ``None`` hint keeps today's any-first-event
# confirmation. Lives here (a low-level module both consumers already import from)
# so the two write paths share one map with no import cycle. The legacy
# WS-subscribe verifier (``tools_service._verify_state_change``) reads it too.


def unwrap_service_response(result: dict[str, Any]) -> dict[str, Any]:
    """Extract service_response from HA call_service result.

    HA's call_service with return_response wraps results in
    {"changed_states": [...], "service_response": {...}}.
    Returns service_response if present and is a dict, otherwise the original result.

    Deliberately NOT the same rule as ``ServiceTools._split_return_response_envelope``,
    which powers ha_call_service: that one returns the response whatever its type and
    reports the whole reply only when the key is absent. The two disagree solely for a
    NON-DICT ``service_response`` (this helper hands back the envelope, the split hands
    back the value). Consumers here read component services that always answer with a
    dict, so the divergence is unreachable — but do not "align" one to the other
    without checking those ~20 call sites.
    """
    sr = result.get("service_response")
    return sr if isinstance(sr, dict) else result


# Fields surfaced from each repair issue. Includes `ignored` / `dismissed_version`
# so callers can distinguish active vs. user-dismissed repairs when both are
# returned (e.g., `include_dismissed_repairs=True`).


# Python logging numeric-level → canonical level name.
# Mirrors the values in HA's LOGSEVERITY constant (components/logger/const.py).


# Error strings produced by the pooled WebSocket path when the transport (not
# the command) fails: the manager's connect raise, send timeouts, and socket
# drops. Callers that attach domain-specific suggestions match on these so an
# HA restart is not presented as a domain problem (issue #1832 review).
#
# Since #1947 ``send_websocket_message`` raises on a dead transport rather than
# collapsing it into ``{"success": False, "error": ...}``, so these signatures
# now only have to catch transport-shaped text that reaches a caller by another
# route (a component-side error frame, or an error string threaded through a
# response body). They are kept as a belt-and-braces match, not as the primary
# detection path.
WS_CONNECTION_SIGNATURES = (
    "failed to connect",
    "timed out",
    "timeout",
    "connection closed",
    # reset_connection: "WebSocket connection to Home Assistant closed while
    # waiting for a response" — "connection closed" is not adjacent there.
    "closed while waiting",
    # listener close reason: "connection dropped without a close frame".
    "connection dropped",
    "disconnected",
    "not connected",
    "not authenticated",
)


def is_connection_error_message(error_msg: Any) -> bool:
    """True when a pooled-WS failure payload is connection/transport-shaped.

    Accepts any payload shape: HA error frames can carry a dict (or None)
    in the error slot, so the value is stringified before matching.
    """
    lowered = str(error_msg).lower()
    return any(sig in lowered for sig in WS_CONNECTION_SIGNATURES)


__all__ = [
    "is_credential_ws_command",
    "normalized_path",
    "raw_ws_refusal",
    "supervisor_path",
    "list_visible_registry_ids",
    "BLOCKED_WS_WRITE_COMMANDS",
    "_SERVICE_TO_STATE",
    "filter_active_repairs",
    "get_logger_levels",
    "is_connection_error_message",
    "is_single_entity_target",
    "merge_visibility_warnings",
    "normalize_log_level",
    "project_repair_fields",
    "summarize_theme_listing",
    "unwrap_service_response",
    "websocket_error_message",
]


async def list_visible_registry_ids(
    client: Any, domain: str, *, bare: bool = False
) -> list[str]:
    """Best-effort list of up to 10 visible ``domain`` entity IDs from the registry.

    Populates the ``available_*_ids`` hint of a RESOURCE_NOT_FOUND error. With
    ``bare`` the ``<domain>.`` prefix is stripped, for tools that take the bare
    storage-key form. Entities hidden by enforce mode are dropped so the hint
    neither leaks a hidden ID nor trips the outbound scan. Returns an empty list
    on any failure: the caller treats absence as "no IDs to report" rather than
    failing the structured error raise. The cap lives here so a new call site
    can't bloat the error payload.
    """
    from ..visibility.enforcement import active_hidden_entity_ids

    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("Failed to list %s entity_ids from registry: %s", domain, e)
        return []
    entries = result.get("result", []) if isinstance(result, dict) else result
    if not isinstance(entries, list):
        return []
    hidden = await active_hidden_entity_ids(client)
    prefix = f"{domain}."
    return [
        entity_id.removeprefix(prefix) if bare else entity_id
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entity_id := entry.get("entity_id"), str)
        and entity_id.startswith(prefix)
        and entity_id not in hidden
    ][:10]
