"""Resolve the helper behind an entity_id for ha_remove_helpers_integrations."""

import logging
from typing import Any

from ha_mcp.backends.legacy_flows import legacy_helper_flow
from ha_mcp.runtime import helper_targets as _shared

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import create_error_response
from .helpers import raise_tool_error, ws_failure_code

FlowLookupReason = _shared.FlowLookupReason
_raise_unexpected_registry_reply = legacy_helper_flow(
    _shared._raise_unexpected_registry_reply
)
get_entry_id_for_flow_helper = legacy_helper_flow(_shared.get_entry_id_for_flow_helper)
_raise_platform_mismatch = legacy_helper_flow(_shared._raise_platform_mismatch)
resolve_helper_entity = legacy_helper_flow(_shared.resolve_helper_entity)


logger = logging.getLogger(__name__)


async def _read_registry_entry(
    client: Any, entity_id: str, warnings: list[str] | None = None
) -> tuple[dict[str, Any] | None, FlowLookupReason]:
    """Read ``entity_id``'s entity-registry entry, or ``(None, reason)``.

    ``reason`` is ``lookup_failed`` for an OSError/TimeoutError from the
    WebSocket send. On Core's ``not_found`` the entity's state decides between
    ``not_registry_managed`` and ``not_in_registry``. A ``success: false``
    reply other than ``not_found``, and a reply without an entry, raise a
    ToolError: neither proves the entity is absent. HomeAssistantConnectionError, HomeAssistantAuthError
    and a non-404 HomeAssistantAPIError from the state read propagate to the
    caller's except chain.
    """
    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
    except (HomeAssistantConnectionError, HomeAssistantAuthError):
        # Typed errors must reach the outer handler — do not swallow.
        raise
    except (OSError, TimeoutError) as e:
        # Network / transport errors from the WS layer (ConnectionError,
        # BrokenPipeError, TimeoutError, …). Programmer-bug-shape
        # exceptions (KeyError, AttributeError, TypeError) intentionally
        # propagate — the response is shape-checked at the dict guard
        # below, and catching them here would mask the bug as a
        # transient WEBSOCKET_DISCONNECTED.
        logger.debug(f"entity_registry/get failed for {entity_id}: {e}")
        if warnings is not None:
            warnings.append(f"entity_registry/get failed for {entity_id}: {e}")
        return None, "lookup_failed"

    if not isinstance(result, dict):
        _raise_unexpected_registry_reply(entity_id, result)
    if not result.get("success"):
        # Core answers an unknown entity with not_found; any other failure (a
        # proxy block, a malformed request) is no evidence of absence.
        if result.get("error_code") != "not_found":
            raise_tool_error(
                create_error_response(
                    ws_failure_code(result),
                    f"Reading the entity registry for {entity_id} failed: "
                    f"{result.get('error') or 'unknown error'}",
                    context={"entity_id": entity_id},
                    suggestions=result.get("suggestions"),
                )
            )
        return None, await _absence_reason(client, entity_id)

    entry = result.get("result")
    if not isinstance(entry, dict) or not entry:
        _raise_unexpected_registry_reply(entity_id, entry)
    return entry, "ok"


async def _absence_reason(client: Any, entity_id: str) -> FlowLookupReason:
    """Tell an entity Core keeps no registry entry for from a missing one.

    Core registers only entities with a ``unique_id``; one without it (a YAML
    template sensor without unique_id, ``zone.home``) still has a state. A
    state read failing with a status other than 404 propagates.
    """
    try:
        state = await client.get_entity_state(entity_id)
    except HomeAssistantAPIError as e:
        if e.status_code != 404:
            raise
        state = None
    return "not_registry_managed" if state else "not_in_registry"


__all__ = [
    "_raise_platform_mismatch",
    "get_entry_id_for_flow_helper",
    "resolve_helper_entity",
]
