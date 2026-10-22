"""
Service discovery tools for Home Assistant MCP server.

This module provides service listing and discovery capabilities,
allowing AI to explore available Home Assistant services/actions.
"""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..runtime.services import ServiceDiscoveryTools as SharedServiceDiscoveryTools
from ..runtime.services import (
    _attach_warnings as _attach_warnings,
)
from ..runtime.services import (
    _build_service_entry as _build_service_entry,
)
from ..runtime.services import (
    _get_entity_type as _get_entity_type,
)
from ..runtime.services import (
    _get_field_type as _get_field_type,
)
from ..runtime.services import (
    _get_number_type as _get_number_type,
)
from ..runtime.services import (
    _get_select_type as _get_select_type,
)
from ..runtime.services import (
    _process_service_fields as _process_service_fields,
)
from ..runtime.services import (
    _process_services as _process_services,
)
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    log_tool_usage,
    register_tool_methods,
)
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

# WS command name for the component's coarse-filtered service catalog read
# (see ``component_api`` module docstring for the capability-negotiation pattern
# this consumer follows).
WS_SERVICES_LIST = "ha_mcp_tools/services_list"


class ServiceDiscoveryTools(SharedServiceDiscoveryTools):
    """Remote compatibility wrapper around the shared implementation."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _fetch_catalog(
        self, domain: str | None, warnings: list[str]
    ) -> tuple[Any, dict[str, Any]]:
        return await _fetch_catalog_and_translations(self._client, domain, warnings)

    ha_list_services = tool(
        name="ha_list_services",
        tags={"Service & Device Control"},
        annotations=read_only_hints("List Available Services", open_world=False),
    )(log_tool_usage(legacy_errors(SharedServiceDiscoveryTools.ha_list_services)))


def register_services_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register service discovery tools with the MCP server."""
    register_tool_methods(mcp, ServiceDiscoveryTools(client))


async def _fetch_catalog_and_translations(
    client: Any, domain: str | None, warnings: list[str] | None = None
) -> tuple[Any, dict[str, Any]]:
    """The ``(rest_services, translations)`` pair ``_process_services`` consumes.

    Tries the component (``domain``-scoped, one frame) first; falls back to the
    legacy REST ``get_services()`` + WS ``frontend/get_translations`` pair on any
    capability miss or fallback-eligible error.
    """
    component_payload = await _fetch_services_list_via_component(client, domain=domain)
    if component_payload is not None:
        return component_payload["services"], component_payload["translations"]
    rest_services = await client.get_services()
    translations = await _get_service_translations(client, warnings)
    return rest_services, translations


async def _fetch_services_list_via_component(
    client: Any, *, domain: str | None
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/services_list`` read; ``None`` ⇒ run the legacy path.

    Returns ``{"services": [...], "translations": {...}}`` — ``services`` is the
    REST ``/api/services`` list shape and ``translations`` the same flat
    ``frontend/get_translations`` resources map ``_get_service_translations``
    returns — so ``_process_services`` consumes either source unchanged. Only
    ``domain`` is threaded to the component (an exact match, identical semantics
    on both sides); ``query`` is deliberately NOT forwarded, even though the
    component also accepts a coarse ``query`` filter, because that coarse pass
    can drop a domain from the payload ENTIRELY when nothing under it matches
    anywhere (name/description/translation) — but ``_process_services``'s
    ``domains`` field is populated purely from ``domain_filter`` (every domain
    that passes it, independent of ``query_filter``), so a query-trimmed payload
    would silently omit domains legacy still lists. Skipping ``query`` on the
    wire keeps ``_process_services`` doing 100% of the query filtering over an
    identical (domain-filtered only) catalog either way, so the output — the
    ``domains`` field included — is byte-identical to legacy in every case, not
    just when a query happens not to trigger the drop. ``None`` on capability
    miss, downgrade (``unknown_command`` → invalidate the cached caps), or
    command error/timeout (logged) — the caller falls back to the legacy REST +
    WS translations fetch.

    Per the uniform transport-fallback taxonomy, a connection-establishment
    failure IS caught here and mapped to ``None`` (legacy fallback), like every
    component fetch helper. This tool's legacy path is the REST ``get_services()`` +
    a per-request WS bridge for translations, NOT the shared pooled WS — so a WS
    outage must not kill the tool when REST can still serve the catalog. The
    catch stays broad so no unexpected fault escapes and kills the tool; routing
    any
    non-command component failure back to the REST catalog fetch is safe here
    (mirrors ``get_component_caps``' own broad-catch precedent).
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "services_list"):
        return None
    kwargs: dict[str, Any] = {"language": "en"}
    if domain is not None:
        kwargs["domain"] = domain
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_SERVICES_LIST, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_SERVICES_LIST, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # DEVIATION (see docstring): the legacy path is REST + a per-request WS
        # bridge, NOT the shared pooled WS. A pooled-WS drop or a failed
        # (re)connect, both HomeAssistantConnectionError since #1947, must fall
        # back to REST rather than kill the tool.
        logger.warning(
            "%s connection error; falling back to REST legacy: %r",
            WS_SERVICES_LIST,
            exc,
        )
        return None
    result = raw.get("result")
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("services"), list)
        or not isinstance(result.get("translations"), dict)
    ):
        logger.debug(
            "%s returned a malformed result (services/translations wrong shape); "
            "falling back to legacy",
            WS_SERVICES_LIST,
        )
        return None
    return result


async def _get_service_translations(
    client: Any, warnings: list[str] | None = None
) -> dict[str, Any]:
    """
    Get service translations from Home Assistant via WebSocket.

    Uses the frontend/get_translations command to retrieve
    human-readable service names and descriptions.
    """
    try:
        response = await client.send_websocket_message(
            {
                "type": "frontend/get_translations",
                "language": "en",
                "category": "services",
            }
        )

        if (
            isinstance(response, dict)
            and response.get("success")
            and response.get("result")
        ):
            result = response["result"]
            if isinstance(result, dict):
                resources: dict[str, Any] = result.get("resources", {})
                return resources
        return {}

    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as e:
        logger.warning(f"Failed to get service translations: {e}")
        # An empty map is also what an instance with no translations returns,
        # so a caller that wants to tell the two apart passes ``warnings``
        # and reports that the catalog came back without descriptions (#1947).
        if warnings is not None:
            warnings.append(f"service descriptions unavailable: {e}")
        return {}
