"""
Configuration management tools for Home Assistant zones.

This module provides tools for listing, creating/updating, and removing
Home Assistant zones (location-based areas for presence automation).
"""

import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
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
from ..errors import ErrorCode, create_error_response
from ..runtime.zone_records import _build_zone_result as _build_zone_result
from ..runtime.zone_records import (
    _shape_component_zone_record as _shape_component_zone_record,
)
from ..runtime.zone_records import (
    _shape_component_zone_rows as _shape_component_zone_rows,
)
from ..runtime.zone_records import _with_registry_icons as _with_registry_icons
from ..runtime.zone_records import _zone_write_response as _zone_write_response
from ..runtime.zone_records import render_zone_snapshot
from ..runtime.zone_writes import ZoneWriteTools
from ..runtime.zones import ZoneReadTools
from ..tool_api import ToolFailure
from .auto_backup import with_auto_backup
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_helpers.create import _execute_create_simple_helper
from .config_helpers.update import _execute_update_simple_helper
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import wait_for_entity_removed

logger = logging.getLogger(__name__)


class ZoneTools(ZoneWriteTools):
    """Zone configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    ha_get_zone = tool(
        name="ha_get_zone",
        tags={"Zones"},
        annotations=read_only_hints("Get Zone", open_world=False),
    )(log_tool_usage(legacy_errors(ZoneReadTools.ha_get_zone)))

    def _map_zone_error(self, error: Exception, zone_id: str | None) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        exception_to_structured_error(
            error,
            context={"zone_id": zone_id},
            suggestions=[
                "Check Home Assistant connection",
                "Verify WebSocket connection is active",
                "Use ha_get_zone() without zone_id to see all available zones",
            ],
        )

    async def _zone_snapshot(self) -> dict[str, Any]:
        caps = await get_component_caps(self._client)
        if component_supports(caps, "helpers_list"):
            snapshot = await self._component_zone_snapshot()
            if snapshot is not None:
                return snapshot
        rows, gaps = await self._legacy_zone_rows()
        return {"rows": rows, "gaps": gaps, "warnings": []}

    async def _get_zone_via_component(
        self, zone_id: str | None
    ) -> dict[str, Any] | None:
        snapshot = await self._component_zone_snapshot()
        return render_zone_snapshot(snapshot, zone_id) if snapshot is not None else None

    async def _component_zone_snapshot(self) -> dict[str, Any] | None:
        try:
            raw = await self._send_component_zone_list()
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as error:
            if is_unknown_command(error):
                invalidate_caps(self._client)
                return None
            logger.warning(
                "ha_mcp_tools/helpers_list (zone) failed; fell back to legacy: %r",
                error,
            )
            rows, gaps = await self._legacy_zone_rows()
            return {
                "rows": rows,
                "gaps": gaps,
                "warnings": [
                    f"component zone listing failed ({error}); served via legacy path"
                ],
            }
        except Exception as error:  # noqa: BLE001 - existing optional remote capability fallback
            logger.warning(
                "ha_mcp_tools/helpers_list (zone) connection error; fell back to legacy: %r",
                error,
            )
            rows, gaps = await self._legacy_zone_rows()
            return {
                "rows": rows,
                "gaps": gaps,
                "warnings": [
                    f"component zone listing connection error ({error}); served via legacy path"
                ],
            }
        result = raw.get("result") or {}
        covered = result.get("covered_types")
        if not (isinstance(covered, list) and "zone" in covered):
            return None
        rows = _shape_component_zone_rows(result)
        try:
            entries, registry_error = await self._zone_registry()
        except HomeAssistantConnectionError as error:
            entries, registry_error = [], str(error)
        warnings = (
            [f"Icons set in the entity registry are missing: {registry_error}"]
            if registry_error
            else []
        )
        return {
            "rows": _with_registry_icons(rows, entries),
            "gaps": [],
            "warnings": warnings,
        }

    async def _send_component_zone_list(self) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/helpers_list`` zone query over the per-client WS."""
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command(
            "ha_mcp_tools/helpers_list",
            helper_types=["zone"],
            include_flow_helpers=False,
        )

    async def _legacy_zone_result(self, zone_id: str | None) -> dict[str, Any]:
        """Serve ha_get_zone from Core's own commands."""
        rows, gaps = await self._legacy_zone_rows()
        return _build_zone_result(rows, zone_id, gaps)

    async def _legacy_zone_rows(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Storage zones from Core's ``zone/list``, then every other zone state.

        A storage row is the stored body with its registry icon; the home
        zone's and YAML zones' rows are their state attributes
        (``friendly_name`` also as ``name``, plus ``persons`` and
        ``device_trackers``). Every row carries ``entity_id``,
        ``editable`` and ``source``, like the component's. The registry links a
        storage zone to its entity (its ``unique_id`` is the zone_id); a zone
        state without such an entry is not stored. Returns the rows and the
        parts that could not be read.
        """
        result = await self._client.send_websocket_message({"type": "zone/list"})
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Failed to get zones"),
                    context={},
                )
            )
        entries, registry_error = await self._zone_registry()
        entity_ids = {e["unique_id"]: e["entity_id"] for e in entries}
        rows = [
            {
                **item,
                "entity_id": entity_ids.get(item.get("id", "")),
                "editable": True,
                "source": "storage",
            }
            for item in result.get("result") or []
            if isinstance(item, dict)
        ]
        rows = _with_registry_icons(rows, entries)
        if registry_error:
            # Without the registry no zone state can be told apart from a stored zone.
            return rows, [
                "Zone entity_ids, icons set in the entity registry, and zones "
                f"outside the zone storage such as 'home', are missing: {registry_error}"
            ]
        try:
            states = await self._client.get_states()
        except (HomeAssistantAPIError, HomeAssistantConnectionError) as exc:
            return rows, [
                f"Zones outside the zone storage such as 'home' are missing: {exc}"
            ]
        stored = set(entity_ids.values())
        for state in states:
            entity_id = state.get("entity_id", "") if isinstance(state, dict) else ""
            if not entity_id.startswith("zone.") or entity_id in stored:
                continue
            attrs = state.get("attributes") or {}
            object_id = entity_id.split(".", 1)[1]
            rows.append(
                {
                    **attrs,
                    "name": attrs.get("friendly_name", object_id),
                    "id": object_id,
                    "entity_id": entity_id,
                    "editable": False,
                    "source": "yaml",
                }
            )
        return rows, []

    async def _zone_registry(self) -> tuple[list[dict[str, Any]], str | None]:
        """The zone integration's registry entries, or the reason they are missing."""
        listed = await self._client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        if not listed.get("success"):
            return [], (
                "the entity registry could not be read: "
                f"{listed.get('error', 'Unknown error')}"
            )
        return [
            e
            for e in listed.get("result") or []
            if isinstance(e, dict)
            and e.get("platform") == "zone"
            and e.get("unique_id")
        ], None

    async def _zone_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            state = await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(state, dict) or not state:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Could not read the state of {entity_id}",
                    context={"entity_id": entity_id},
                )
            )
        return state

    async def _zone_target_snapshot(self, zone_id: str) -> dict[str, Any]:
        listed = await self._client.send_websocket_message({"type": "zone/list"})
        if not listed.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Could not find zone {zone_id}: {listed.get('error', 'Failed to get zones')}",
                    context={"zone_id": zone_id},
                )
            )
        entries, registry_error = await self._zone_registry()
        if registry_error:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Could not find zone {zone_id}: {registry_error}",
                    context={"zone_id": zone_id},
                )
            )
        return {"items": listed.get("result") or [], "entries": entries}

    async def _create_zone(self, *args: Any) -> dict[str, Any]:
        return await _execute_create_simple_helper(self._client, *args)

    async def _update_zone(self, *args: Any) -> dict[str, Any]:
        return await _execute_update_simple_helper(self._client, *args)

    async def _remove_stored_zone(
        self, storage_id: str, entity_id: str | None
    ) -> dict[str, Any]:
        return await self._client.send_websocket_message(
            {"type": "zone/delete", "zone_id": storage_id}
        )

    async def _wait_zone_removed(self, entity_id: str) -> bool:
        try:
            return await wait_for_entity_removed(self._client, entity_id)
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as error:
            raise ToolFailure("VERIFICATION_FAILED", str(error)) from error

    def _map_zone_write_error(
        self, error: Exception, zone_id: str | None, operation: str | None
    ) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        context: dict[str, Any] = {"zone_id": zone_id}
        if operation is not None:
            context["operation"] = operation
        exception_to_structured_error(
            error,
            context=context,
            suggestions=[
                "Check Home Assistant connection",
                "Verify coordinates are valid"
                if operation == "create"
                else "Verify zone_id exists using ha_get_zone()",
            ],
        )

    ha_set_zone = tool(
        name="ha_set_zone",
        tags={"Zones"},
        annotations=write_hints(
            "Set Zone", destructive=True, idempotent=False, open_world=False
        ),
    )(
        with_auto_backup(domain="zone", id_param="zone_id")(
            log_tool_usage(legacy_errors(ZoneWriteTools.ha_set_zone))
        )
    )

    ha_remove_zone = tool(
        name="ha_remove_zone",
        tags={"Zones"},
        annotations=write_hints(
            "Remove Zone", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="zone", id_param="zone_id")(
            log_tool_usage(legacy_errors(ZoneWriteTools.ha_remove_zone))
        )
    )


def register_zone_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant zone configuration tools."""
    register_tool_methods(mcp, ZoneTools(client))
