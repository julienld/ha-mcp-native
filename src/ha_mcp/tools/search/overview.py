"""System overview collection for ``ha_get_overview``.

Input and slice containers, the field partitions and the collection methods.
"""

import logging
from dataclasses import dataclass
from typing import Any, cast

from ...client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ...client.websocket_client import get_websocket_client
from ...runtime.overview_sections import (
    apply_overview_notifications,
    apply_overview_repairs,
    apply_overview_system_info,
)
from ...runtime.overview_values import (
    _OVERVIEW_AVAILABLE_FIELDS as _OVERVIEW_AVAILABLE_FIELDS,
)
from ...runtime.overview_values import (
    _OVERVIEW_ENTITY_FIELDS as _OVERVIEW_ENTITY_FIELDS,
)
from ...runtime.overview_values import (
    _OVERVIEW_INDEPENDENT_FIELDS as _OVERVIEW_INDEPENDENT_FIELDS,
)
from ...runtime.overview_values import (
    _OVERVIEW_NOTIFICATION_FIELDS as _OVERVIEW_NOTIFICATION_FIELDS,
)
from ...runtime.overview_values import (
    _OVERVIEW_REPAIR_FIELDS as _OVERVIEW_REPAIR_FIELDS,
)
from ...runtime.overview_values import (
    _OverviewInputs as _OverviewInputs,
)
from ..component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .base import SearchToolsBase

__all__ = [
    "_OVERVIEW_AVAILABLE_FIELDS",
    "_OVERVIEW_ENTITY_FIELDS",
    "_OVERVIEW_INDEPENDENT_FIELDS",
    "OverviewMixin",
    "_OverviewInputs",
    "logger",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _OverviewSlices:
    """The component's raw overview slices, adapted to the assembly's shapes.

    ``registry_slices`` bundles the five ``get_system_overview`` inputs — bare
    ``states`` / ``services`` lists plus the three registries re-wrapped in the
    ``{success, result}`` envelope ``_extract_registry_list`` / ``load_hidden_set``
    unwrap. ``config`` is the bare ``get_config()`` dict. ``notifications`` and
    ``repairs`` are re-wrapped in the WS ``{success, result}`` envelope the
    ``_fetch_*`` helpers unwrap (``repairs`` nested under ``result.issues``).
    """

    registry_slices: dict[str, Any]
    config: dict[str, Any]
    notifications: dict[str, Any]
    repairs: dict[str, Any]


# These disjoint sets partition every key documented by ha_get_overview's
# fields parameter. Keep the manifest test in sync when the public schema changes.


def _build_component_overview_request(inputs: _OverviewInputs) -> dict[str, Any]:
    """Translate resolved ha_get_overview inputs into an ``ha_mcp_tools/overview`` request.

    The component returns raw slices independent of the display params
    (``detail_level`` / ``domains`` / ``limit`` / ``offset`` /
    ``max_entities_per_domain`` / ``include_state`` / ``include_entity_id`` stay
    server-side — the server assembles), so only the two fetch-gating flags cross
    the wire: ``include_notifications`` mirrors the wrapper's flag;
    ``include_repairs`` is always ``True`` because the wrapper always assembles
    repairs (``include_dismissed_repairs`` only filters dismissed ones
    server-side, it never skips the fetch).
    """
    return {
        "include_notifications": inputs.include_notifications,
        "include_repairs": True,
    }


def _wrap_registry(slice_value: Any) -> dict[str, Any]:
    """Wrap a bare registry list in the ``{success, result}`` envelope the assembly expects."""
    return {
        "success": True,
        "result": slice_value if isinstance(slice_value, list) else [],
    }


# The always-present overview slices the component returns independent of any
# request flag. Each must be a list; ``config`` (a dict) is checked separately.
# A missing/malformed member means the component couldn't assemble a trustworthy
# snapshot, so the caller falls back to the legacy fetch path rather than serve a
# silently-degraded overview.
_REQUIRED_OVERVIEW_LIST_SLICES = (
    "states",
    "services",
    "area_registry",
    "entity_registry",
    "device_registry",
)


def _build_overview_slices(component_result: dict[str, Any]) -> _OverviewSlices | None:
    """Adapt the component's BARE overview slices into the assembly's shapes.

    The component returns bare in-process data (no ``{success, result}`` WS
    wrapper — design § ha_mcp_tools/overview); the server's ``get_system_overview``
    + ``_fetch_*`` were written against the wrapped REST/WS payloads, so the three
    registries and the notifications/repairs reads are re-wrapped here at the
    seam. ``states`` / ``services`` / ``config`` already match their bare
    ``get_states()`` / ``get_services()`` / ``get_config()`` shapes.

    Returns ``None`` (⇒ legacy fallback) when the snapshot can't be trusted: any
    required slice missing/malformed (see ``_REQUIRED_OVERVIEW_LIST_SLICES`` plus
    ``config``), or the component reported a non-empty ``slice_errors`` list (a
    per-slice read failure it surfaced instead of silently emptying). The
    flag-gated ``notifications`` / ``repairs`` slices stay lenient — absent or
    malformed degrades to empty, matching a request that never asked for them.
    """
    result = component_result if isinstance(component_result, dict) else {}

    slice_errors = result.get("slice_errors")
    if isinstance(slice_errors, list) and slice_errors:
        return None
    for key in _REQUIRED_OVERVIEW_LIST_SLICES:
        if not isinstance(result.get(key), list):
            return None
    config = result.get("config")
    if not isinstance(config, dict):
        return None

    notifications = result.get("notifications")
    repairs = result.get("repairs")
    return _OverviewSlices(
        registry_slices={
            "states": result["states"],
            "services": result["services"],
            "area_registry": _wrap_registry(result["area_registry"]),
            "entity_registry": _wrap_registry(result["entity_registry"]),
            "device_registry": _wrap_registry(result["device_registry"]),
        },
        config=config,
        notifications={
            "success": True,
            "result": notifications if isinstance(notifications, list) else [],
        },
        repairs={
            "success": True,
            "result": {"issues": repairs if isinstance(repairs, list) else []},
        },
    )


class OverviewMixin(SearchToolsBase):
    """Collects and assembles the ``ha_get_overview`` response."""

    async def _collect_independent_overview(
        self,
        *,
        requested_fields: set[str],
        detail_level: str,
        include_notifications: bool,
        include_dismissed_repairs: bool,
    ) -> dict[str, Any]:
        """Collect requested independent sections with full-path error semantics."""
        result: dict[str, Any] = {"success": True}
        if "system_info" in requested_fields:
            await self._fetch_system_info(result, detail_level)
        if requested_fields & _OVERVIEW_NOTIFICATION_FIELDS:
            if include_notifications:
                await self._fetch_notifications(result)
            else:
                result.setdefault("warnings", []).append(
                    "notifications omitted: include_notifications=False"
                )
        if requested_fields & _OVERVIEW_REPAIR_FIELDS:
            await self._fetch_repairs(
                result,
                include_dismissed_repairs,
            )
        return result

    async def _fetch_system_info(
        self,
        result: dict[str, Any],
        detail_level: str,
        *,
        prefetched_config: dict[str, Any] | None = None,
    ) -> None:
        """Populate result['system_info'] from HA config, warning on failure.

        ``prefetched_config`` (the component's ``config`` slice, already the bare
        ``get_config()`` dict) is used verbatim when given, skipping the fetch.
        """
        try:
            config = (
                prefetched_config
                if prefetched_config is not None
                else await self._client.get_config()
            )
            apply_overview_system_info(
                result, config, self._client.base_url, detail_level
            )
        except Exception as e:
            logger.warning(
                "Failed to fetch system info for overview: %s", e, exc_info=True
            )
            result.setdefault("warnings", []).append(f"system info unavailable: {e}")
            if "system_summary" in result:
                result["system_summary"].setdefault("version", "unknown")

    async def _fetch_notifications(
        self,
        result: dict[str, Any],
        *,
        prefetched_notifications: dict[str, Any] | None = None,
    ) -> None:
        """Attach active persistent notifications, warning on failure.

        ``prefetched_notifications`` (the component's ``notifications`` slice
        re-wrapped in the ``{success, result}`` envelope) is unwrapped by the same
        code as the live fetch when given, skipping the WS call.
        """
        result["notification_count"] = 0
        result["notifications"] = []
        try:
            ws_result = (
                prefetched_notifications
                if prefetched_notifications is not None
                else await self._client.send_websocket_message(
                    {"type": "persistent_notification/get"}
                )
            )
            if ws_result.get("success"):
                notifications = ws_result.get("result", [])
                apply_overview_notifications(result, notifications)
            else:
                # HA answered and rejected. The pre-seeded zero would otherwise
                # report "none pending" for a section that never ran, the same
                # false negative the transport path already reports.
                err = ws_result.get("error")
                err_msg = (
                    err.get("message") if isinstance(err, dict) else err
                ) or "unknown error"
                result.setdefault("warnings", []).append(
                    f"notifications unavailable: {err_msg}"
                )
        except Exception as e:
            logger.warning(
                "Failed to fetch notifications for overview: %s", e, exc_info=True
            )
            # Leaving the keys off entirely reads as "no notifications", which
            # is a different answer from "could not ask" (#1947).
            result.setdefault("warnings", []).append(f"notifications unavailable: {e}")

    async def _fetch_repairs(
        self,
        result: dict[str, Any],
        include_dismissed_repairs_bool: bool,
        *,
        prefetched_repairs: dict[str, Any] | None = None,
    ) -> None:
        """Attach active repairs issues, recording failures in repairs_error.

        ``prefetched_repairs`` (the component's ``repairs`` slice re-wrapped in the
        ``{success, result: {issues: [...]}}`` envelope) is unwrapped, filtered
        (``filter_active_repairs``), and projected by the same code as the live
        fetch when given, skipping the WS call.
        """
        result["repair_count"] = 0
        result["repairs"] = []
        try:
            repairs_result = (
                prefetched_repairs
                if prefetched_repairs is not None
                else await self._client.send_websocket_message(
                    {"type": "repairs/list_issues"}
                )
            )
            if repairs_result.get("success"):
                raw_issues = repairs_result.get("result", {}).get("issues", [])
                # Core's ``repairs/list_issues`` filters ``if issue.active``; the
                # component's ``overview`` repairs slice does NOT (it emits every
                # registry issue, carrying ``active`` additively). After an HA
                # restart the registry restores previously-reported issues as
                # ``active=False`` placeholders the legacy path and Repairs UI
                # never show, so drop them here for parity. Legacy rows omit
                # ``active`` (None) → no-op for them.
                apply_overview_repairs(
                    result,
                    raw_issues,
                    include_dismissed_repairs_bool,
                )
            else:
                err = repairs_result.get("error") or {}
                err_msg = (
                    err.get("message") if isinstance(err, dict) else str(err)
                ) or "unknown error"
                logger.warning(
                    "repairs/list_issues returned success=false: %s", err_msg
                )
                result["repairs_error"] = f"Could not fetch repairs: {err_msg}"
        except Exception as e:
            logger.warning("Failed to fetch repairs for overview: %s", e, exc_info=True)
            result["repairs_error"] = f"Could not fetch repairs: {e}"

    async def _collect_overview(self, inputs: _OverviewInputs) -> dict[str, Any]:
        """Assemble the HA-sourced overview, preferring the in-process component.

        The component's ``ha_mcp_tools/overview`` returns the eight raw reads the
        legacy path makes today (states + services + the three registries +
        config + notifications + repairs) in one WebSocket round-trip; the server
        feeds those slices into its **unchanged** assembly
        (``get_system_overview`` + ``system_info`` / ``notifications`` /
        ``repairs``), so the two paths are byte-identical by construction. Routed
        all-or-nothing per the ``ha_search`` precedent, gated solely on the
        ``overview`` capability. Unlike ``ha_search``, an active
        entity-visibility filter does NOT force the legacy path here:
        ``get_system_overview`` calls ``load_hidden_set`` unconditionally and
        ``_build_overview_slices`` hands it the same registry + states envelope
        the legacy fetch produces, so the filter is applied server-side over the
        component's slices and the output stays byte-identical to legacy
        (``load_hidden_set`` reaches any extra data it needs — e.g. the
        Assist-exposure dimension — through the ``client`` it is already passed).
        The server-side-only fields (``tool_discovery``, ``settings_url``,
        ``read_only_mode``, ``ha_mcp_update``) and the ``fields=`` projection are
        applied by ``ha_get_overview`` after this, identically on both paths.
        """
        caps = await get_component_caps(self._client)
        if component_supports(caps, "overview") and component_supports(
            caps, DEVICE_REGISTRY_CHILD_SEMANTICS
        ):
            component_result = await self._overview_via_component(inputs)
            if component_result is not None:
                return component_result
        return await self._assemble_overview(inputs, None)

    async def _overview_via_component(
        self, inputs: _OverviewInputs
    ) -> dict[str, Any] | None:
        """Serve the overview from the component's slices; ``None`` ⇒ run legacy.

        Error taxonomy (design § 4), mirroring ``_ha_search_via_component``:

        - ``unknown_command`` (the component was downgraded mid-session, so the
          cached positive caps are stale): invalidate the caps and return
          ``None`` so the caller falls back **silently** — an expected,
          non-actionable transition.
        - any other ``HomeAssistantCommandError`` (a component handler bug) or a
          ``HomeAssistantCommandTimeout`` (the component WS overview timed out):
          serve the correct result from the legacy path, append a ``warnings[]``
          entry, and ``log.warning`` — correct results now, breakage visible.
        - a malformed slice payload (a required slice missing/malformed, or a
          non-empty ``slice_errors`` — ``_build_overview_slices`` returns
          ``None``): treated like the command-error branch (legacy + warning +
          log), so a partial snapshot never serves a silently-degraded overview.
        - ``HomeAssistantConnectionError`` (pooled-WS drop) or the plain
          ``Exception`` ``get_websocket_client()`` raises on a failed (re)connect:
          served the same way — the legacy overview reads ``/api/states`` +
          ``/api/services`` over REST and the registries through the swallowing
          ``send_websocket_message`` bridge, so it degrades to a partial overview
          rather than dying identically on a pooled-WS drop; a transport failure
          must not escape.
        """
        try:
            raw = await self._send_component_overview(inputs)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            legacy = await self._assemble_overview(inputs, None)
            legacy.setdefault("warnings", []).append(
                f"component overview path failed ({exc}); served via legacy path"
            )
            logger.warning("ha_mcp_tools/overview failed; fell back to legacy: %r", exc)
            return legacy
        except Exception as exc:  # noqa: BLE001
            legacy = await self._assemble_overview(inputs, None)
            legacy.setdefault("warnings", []).append(
                f"component overview connection error ({exc}); served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/overview connection error; fell back to legacy: %r", exc
            )
            return legacy
        slices = _build_overview_slices(raw.get("result") or {})
        if slices is None:
            legacy = await self._assemble_overview(inputs, None)
            legacy.setdefault("warnings", []).append(
                "component overview returned malformed slices; served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/overview returned malformed slices; fell back to legacy"
            )
            return legacy
        return await self._assemble_overview(inputs, slices)

    async def _send_component_overview(self, inputs: _OverviewInputs) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/overview`` command over the per-client WebSocket."""
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command(
            "ha_mcp_tools/overview",
            **_build_component_overview_request(inputs),
        )

    async def _assemble_overview(
        self, inputs: _OverviewInputs, prefetched: _OverviewSlices | None
    ) -> dict[str, Any]:
        """Assemble the overview from slices — prefetched or legacy-fetched.

        Identical assembly either way: ``get_system_overview``'s join plus the
        wrapper's ``system_info`` / ``notifications`` / ``repairs``, with the
        entity-visibility filter applied server-side over the (prefetched or
        fetched) registry + states. ``prefetched`` carries the component's raw
        slices adapted to the shapes the assembly consumes; ``None`` runs the
        original per-read REST/WS fetches. Byte-parity between the two is by
        construction — same code, only the data source differs.
        """
        result = await self._smart_tools.get_system_overview(
            inputs.detail_level,
            inputs.max_entities_per_domain,
            inputs.include_state,
            inputs.include_entity_id,
            domains_filter=inputs.domains_filter,
            limit=inputs.limit,
            offset=inputs.offset,
            prefetched_slices=prefetched.registry_slices if prefetched else None,
        )
        result = cast(dict[str, Any], result)

        await self._fetch_system_info(
            result,
            inputs.detail_level,
            prefetched_config=prefetched.config if prefetched else None,
        )

        if inputs.include_notifications:
            await self._fetch_notifications(
                result,
                prefetched_notifications=(
                    prefetched.notifications if prefetched else None
                ),
            )

        await self._fetch_repairs(
            result,
            inputs.include_dismissed_repairs,
            prefetched_repairs=prefetched.repairs if prefetched else None,
        )
        return result
