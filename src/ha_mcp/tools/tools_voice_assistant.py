"""
Voice Assistant Exposure Query Tools for Home Assistant.

This module provides tools for querying entity exposure to voice assistants
(Alexa, Google Home, Assist). To modify exposure, use ha_set_entity(expose_to=...).

Known assistant identifiers:
- "conversation" - Home Assistant Assist (local voice control)
- "cloud.alexa" - Alexa via Nabu Casa cloud
- "cloud.google_assistant" - Google Assistant via Nabu Casa cloud
"""

import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from ..runtime.exposure import _EXPOSURE_ENRICHMENT_KEYS as _EXPOSURE_ENRICHMENT_KEYS
from ..runtime.exposure import KNOWN_ASSISTANTS as KNOWN_ASSISTANTS
from ..runtime.exposure import ExposureTools
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

# Known voice assistant identifiers in Home Assistant

# The ha_mcp_tools/exposure WS command: the legacy expose_entity/list map PLUS a
# sibling entity_info enrichment (names/areas). Named once so the routing helper
# and its tests agree on the wire string.
WS_EXPOSURE = "ha_mcp_tools/exposure"

# Additive keys the component's exposure enrichment (entity_info) contributes to a
# single-entity response. friendly_name / state are live-state fields (omitted by
# the component when the entity has no state); domain / area / floor / labels are
# registry-derived and always present. Merged strictly on top of the byte-identical
# legacy keys (exposed_to / is_exposed_anywhere / has_custom_settings / note).


class VoiceAssistantTools(ExposureTools):
    """Voice assistant exposure query tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _fetch_exposure_via_component(
        self, entity_id: str | None
    ) -> dict[str, Any] | None:
        """One ``ha_mcp_tools/exposure`` read; ``None`` ⇒ run the legacy path.

        Returns the component payload — ``{exposed_entities: {id: {assistant:
        True}}, entity_info: {id: {...}}}`` — where ``exposed_entities`` is
        byte-identical to the legacy ``homeassistant/expose_entity/list`` map so
        the existing ``_get_entity_exposure`` / ``_list_exposures`` shapers consume
        it unchanged. ``None`` on capability miss, downgrade (``unknown_command`` →
        invalidate the cached caps), command error/timeout (logged), or a
        connection-establishment failure (logged) — the caller falls back to the
        legacy WS list. A ``HomeAssistantConnectionError`` — a pooled-WS drop,
        or a failed (re)connect — is caught here and mapped to ``None``: the
        legacy ``homeassistant/expose_entity/list`` read rides the
        ``send_websocket_message`` bridge, which answers a component-side fault
        with ``{"success": False}`` - so a fault here falls back rather than
        escapes. It is the SAME pooled connection, so a genuinely dead
        transport raises there too (#1947) instead of degrading. Same caps-gate discipline as
        ``component_devices.fetch_device_via_component``.
        """
        caps = await get_component_caps(self._client)
        if not (
            component_supports(caps, "exposure")
            and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
        ):
            return None
        kwargs: dict[str, Any] = {}
        if entity_id is not None:
            kwargs["entity_id"] = entity_id
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
            raw = await ws.send_command(WS_EXPOSURE, **kwargs)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning("%s failed; fell back to legacy: %r", WS_EXPOSURE, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            # HomeAssistantConnectionError / plain establish Exception → legacy (the
            # legacy expose_entity/list read rides the bridge).
            logger.warning(
                "%s connection error; falling back to legacy: %r", WS_EXPOSURE, exc
            )
            return None
        result = raw.get("result")
        if not isinstance(result, dict) or "exposed_entities" not in result:
            return None
        return result

    async def _fetch_exposure_payload(self, entity_id: str | None) -> dict[str, Any]:
        component = await self._fetch_exposure_via_component(entity_id)
        if component is not None:
            return component
        result = await self._client.send_websocket_message(
            {"type": "homeassistant/expose_entity/list"}
        )
        if not result.get("success"):
            error = result.get("error", {})
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to get exposure settings: {error_msg}",
                    context={"entity_id": entity_id},
                )
            )
        return {
            "exposed_entities": result.get("result", {}).get("exposed_entities", {})
        }

    def _raise_exposure_error(
        self, error: Exception, entity_id: str | None
    ) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        exception_to_structured_error(error, context={"entity_id": entity_id})

    ha_get_entity_exposure = tool(
        name="ha_get_entity_exposure",
        tags={"Entity Registry"},
        annotations=read_only_hints("Get Entity Exposure", open_world=False),
    )(log_tool_usage(legacy_errors(ExposureTools.ha_get_entity_exposure)))


def register_voice_assistant_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register voice assistant exposure query tools."""
    register_tool_methods(mcp, VoiceAssistantTools(client))
