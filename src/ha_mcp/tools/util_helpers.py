"""
Shared utility functions for MCP tool modules.

This module provides common helper functions used across multiple tool registration modules.
"""

import logging
from typing import Any

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime.errors import websocket_error_message as websocket_error_message
from ha_mcp.runtime.health_results import (
    _REPAIR_PROJECTION_FIELDS as _REPAIR_PROJECTION_FIELDS,
)
from ha_mcp.runtime.health_results import filter_active_repairs as filter_active_repairs
from ha_mcp.runtime.health_results import project_repair_fields as project_repair_fields
from ha_mcp.runtime.health_results import (
    summarize_theme_listing as summarize_theme_listing,
)
from ha_mcp.runtime.integration_logs import get_logger_levels as _shared_logger_levels
from ha_mcp.runtime.integration_logs import normalize_log_level as normalize_log_level
from ha_mcp.runtime.search_response import (
    merge_visibility_warnings as merge_visibility_warnings,
)

from ..runtime.action_constants import _SERVICE_TO_STATE as _SERVICE_TO_STATE
from ..runtime.action_constants import (
    is_single_entity_target as is_single_entity_target,
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


# WebSocket commands that mutate persistent state in a way that bypasses a
# wrapping MCP tool's validation (auto-backup, config-hash optimistic locking,
# registry invariant checks), or that have no escape-hatch-appropriate use case
# (`config/core/update` rewrites the installation's location/timezone/currency/
# lat-long). Shared by the code sandbox's `ws_send` bridge (tools_code.py) and
# ha_call_service's `ws_command` escape hatch (tools_service.py) so the two raw
# WebSocket surfaces stay in lockstep. Registry deletion command names differ on
# HA Core: device deletion is `remove_config_entry` and entity deletion is
# `remove` (not `delete`) -- ha_remove_device / ha_remove_entity emit those.
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
