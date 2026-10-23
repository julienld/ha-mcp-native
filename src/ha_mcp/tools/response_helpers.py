"""Response shaping helpers for tool results.

Field projection, pagination metadata, and Home Assistant timezone lookup with
timestamp localisation.
"""

import logging
from typing import Any

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..runtime.projection import (
    build_pagination_metadata,
    compact_service_result,
    project_entity_record,
    project_fields,
    public_fields,
    resolve_local_timezone,
    timezone_metadata,
)
from .component_api import get_component_caps

logger = logging.getLogger(__name__)


# Default compact-result projection for ha_call_service (issue #1446). A single
# WLED light's `effect_list` can be ~250 entries, emitted on every propagated
# group state — `light.turn_on` on a nested group returned 16 state objects
# with that list carried four times. Drops timestamp/context metadata at the
# top level and known-heavy enum-style attribute lists.
#
# Extension policy: add a key here only when (a) it's universally heavy across
# installations (not just an unusual config), (b) it carries no signal callers
# would act on for service-call confirmation, and (c) callers who do want it
# can opt in via `result_fields` / `verbose=True`. Domain-specific or
# install-specific trimming belongs in `ha_get_state` via explicit
# `attribute_keys`, not here.


async def fetch_ha_timezone(client: Any) -> tuple[str, bool]:
    """Fetch the HA timezone, preferring the ``ha_mcp_tools`` component's cached
    ``info`` handshake over a fresh ``/api/config`` REST call.

    When ``get_component_caps(client)`` reports a non-empty ``timezone`` (an
    additive ``info`` field — see ``ComponentCaps.timezone``), return it
    directly with NO REST call. Otherwise falls back to the legacy
    ``client.get_config()`` fetch exactly as before — the path taken when the
    component is absent, predates the ``timezone`` field, or reports it empty.

    Staleness trade-off: the component route reads a cached, process-lifetime
    probe, so an HA timezone change mid-session keeps serving the value from
    the last successful negotiation (positive cache entries do not expire on a
    timer — only ``invalidate_caps`` or a process restart forces a re-probe)
    until then. The #1813 Phase 2 audit rated this Low risk and acceptable —
    instance timezone changes are rare — versus the legacy path, which always
    re-fetches fresh on every call.

    Returns ``(ha_timezone, fetch_failed)``. ``fetch_failed`` is ``True`` only
    when the legacy REST fetch raised, in which case *ha_timezone* is always
    ``"UTC"``.
    """
    caps = await get_component_caps(client)
    if caps is not None and caps.timezone:
        return caps.timezone, False

    try:
        config = await client.get_config()
        return config.get("time_zone", "UTC"), False
    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as _tz_exc:
        logger.warning(
            "add_timezone_metadata: failed to fetch HA timezone config — "
            "falling back to UTC: %s",
            _tz_exc,
            exc_info=True,
        )
        return "UTC", True


async def add_timezone_metadata(
    client: Any,
    data: dict[str, Any],
    include_metadata: bool = True,
    *,
    convert_timestamps: bool = True,
) -> dict[str, Any]:
    """Add Home Assistant timezone to tool responses and convert timestamps to local time.

    Resolves the Home Assistant time zone via ``fetch_ha_timezone`` (which
    prefers the ``ha_mcp_tools`` component's cached handshake and falls back to
    ``/api/config``), converts every ``last_changed``, ``last_updated``,
    ``last_reported``, ``when``, and ``last_triggered`` field found anywhere in
    *data* from UTC to that local timezone, then wraps the result in
    ``{"data": ..., "metadata": {...}}``.

    Pass ``include_metadata=False`` to return *data* unchanged — the
    ``metadata`` wrapper is then omitted entirely.

    Pass ``convert_timestamps=False`` to retain native values while still
    including timezone context, without claiming that timestamps were converted.

    Conversion notes:
    - Offset-aware strings (``+00:00``) are converted directly.
    - Naive strings (no offset) are assumed to be UTC before conversion.
    - ``ZoneInfoNotFoundError`` (tzdata not installed) falls back to UTC.
    - Any config-fetch failure also falls back to UTC without conversion.
    """
    if not include_metadata:
        return data
    ha_timezone, fetch_failed = await fetch_ha_timezone(client)
    return timezone_metadata(
        data,
        ha_timezone,
        fetch_failed,
        include_metadata,
        convert_timestamps=convert_timestamps,
    )


__all__ = [
    "add_timezone_metadata",
    "build_pagination_metadata",
    "compact_service_result",
    "fetch_ha_timezone",
    "project_entity_record",
    "project_fields",
    "public_fields",
    "resolve_local_timezone",
]
