"""
Configuration management tools for Home Assistant zones.

This module provides tools for listing, creating/updating, and removing
Home Assistant zones (location-based areas for presence automation).
"""

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..backup_helper_target import stored_target
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response, create_validation_error
from ..runtime.zone_records import _build_zone_result as _build_zone_result
from ..runtime.zone_records import (
    _shape_component_zone_record as _shape_component_zone_record,
)
from ..runtime.zone_records import (
    _shape_component_zone_rows as _shape_component_zone_rows,
)
from ..runtime.zone_records import _with_registry_icons as _with_registry_icons
from ..runtime.zone_records import render_zone_snapshot
from ..runtime.zones import ZoneReadTools
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
    validate_identifier_not_empty,
    ws_failure_code,
)
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import wait_for_entity_removed

logger = logging.getLogger(__name__)


class ZoneTools(ZoneReadTools):
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

    async def _resolve_zone(self, zone_id: str) -> tuple[str | None, str, str | None]:
        """The entity_id, storage zone_id and registry icon of a stored zone,
        given its entity_id or zone_id.

        Resolved as ``stored_target`` resolves it, and so as the zone backups
        do: a bare ``zone_id`` is a storage id from ``zone/list``, and
        ``zone.<name>`` is the zone whose registry entry has that entity_id. A
        renamed entity can carry another zone's id as its name, so a bare id
        never names an entity, and a repeated removal deletes nothing.
        Both entity_id and icon are None for a stored zone without a registry
        entry; the icon is also None when the entry sets none. The
        home zone and YAML zones are not in the storage; Core gives them no
        ``unique_id``, so they have no registry entry.
        """
        listed = await self._client.send_websocket_message({"type": "zone/list"})
        if not listed.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Could not find zone {zone_id}: "
                    f"{listed.get('error', 'Failed to get zones')}",
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
        stored = {str(zone.get("id")) for zone in listed.get("result") or []}
        storage_id, entity = stored_target("zone", zone_id, stored, entries)
        if storage_id in stored:
            icon = next(
                (e.get("icon") for e in entries if e.get("unique_id") == storage_id),
                None,
            )
            return entity, storage_id, icon
        entity_id = zone_id if zone_id.startswith("zone.") else f"zone.{zone_id}"
        # A registry entry links a stored zone, so only a state without one is
        # a YAML or the home zone.
        linked = any(entry.get("entity_id") == entity_id for entry in entries)
        if not linked and await self._zone_state(entity_id) is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Zone {entity_id} is not a stored zone: it is defined in YAML "
                    "or, for the default zone.home, by the home location in the "
                    "general settings, so the zone tools cannot change or remove it.",
                    context={"zone_id": zone_id, "entity_id": entity_id},
                    suggestions=[
                        "Edit a YAML zone in configuration.yaml",
                        "Change the home location under Settings > System > General",
                    ],
                )
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Zone not found: {zone_id}",
                context={"zone_id": zone_id},
                suggestions=[
                    "Use ha_get_zone() without zone_id to see all available zones",
                    (
                        "A zone_id without 'zone.' is a storage id; name a zone "
                        "by its entity with the full entity_id"
                    ),
                ],
            )
        )

    @tool(
        name="ha_set_zone",
        tags={"Zones"},
        annotations=write_hints(
            "Set Zone", destructive=True, idempotent=False, open_world=False
        ),
    )
    @with_auto_backup(domain="zone", id_param="zone_id")
    @log_tool_usage
    async def ha_set_zone(
        self,
        name: Annotated[
            str | None,
            Field(
                description="Display name for the zone",
                default=None,
            ),
        ] = None,
        latitude: Annotated[
            float | None,
            Field(
                description="Latitude coordinate of the zone center",
                default=None,
            ),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(
                description="Longitude coordinate of the zone center",
                default=None,
            ),
        ] = None,
        zone_id: Annotated[
            str | None,
            Field(
                description="Zone ID or entity_id of the zone to update (from ha_get_zone)",
                default=None,
            ),
        ] = None,
        radius: Annotated[
            float | None,
            Field(
                description="Radius of the zone in meters; Home Assistant uses 100 when it is omitted on create",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:briefcase'). On update, '' clears it, except an icon stored in the zone itself (as the Home Assistant UI stores it; ha_get_zone shows it), which cannot be removed",
                default=None,
            ),
        ] = None,
        passive: Annotated[
            bool | None,
            Field(
                description="Passive mode: the zone is hidden in the frontend and not used for device tracker state, but automations can still use it (false when omitted on create)",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for the zone to be queryable before returning. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant zone.

        Omit zone_id to create a new zone (name, latitude, longitude required).
        Provide zone_id to update an existing zone (only specified fields change).
        Zones defined in YAML, and the home zone, cannot be changed here.

        EXAMPLES:
        - Create: ha_set_zone(name="Office", latitude=40.7128, longitude=-74.0060, radius=150, icon="mdi:briefcase")
        - Update: ha_set_zone(zone_id="office", radius=200)
        - Update by entity_id: ha_set_zone(zone_id="zone.office", radius=200)
        """
        operation = "create"
        try:
            # ``None`` stays the documented "create-new" sentinel; explicit
            # empty/whitespace ``zone_id`` would silently route to the
            # create branch below and surface "name, latitude, longitude
            # required" instead of the actual cause (unusable ``zone_id``).
            if zone_id is not None:
                validate_identifier_not_empty(
                    zone_id,
                    "zone_id",
                    suggestions=[
                        "Omit zone_id entirely to create a new zone",
                        "Pass a valid zone_id to update an existing zone",
                    ],
                    context={"action": "set"},
                )
            fields = {
                key: value
                for key, value in (
                    ("latitude", latitude),
                    ("longitude", longitude),
                    ("radius", radius),
                    ("passive", passive),
                )
                if value is not None
            }
            if name is not None and not name.strip():
                raise_tool_error(
                    create_validation_error(
                        "name cannot be blank.", context={"zone_id": zone_id}
                    )
                )
            if zone_id:
                operation = "update"
                updated = [
                    key
                    for key, value in (
                        ("name", name),
                        ("latitude", latitude),
                        ("longitude", longitude),
                        ("radius", radius),
                        ("icon", icon),
                        ("passive", passive),
                    )
                    if value is not None
                ]
                if not updated:
                    raise_tool_error(
                        create_validation_error(
                            "No fields to update. Provide at least one field to change.",
                            context={"zone_id": zone_id},
                        )
                    )
                entity_id, storage_id, registry_icon = await self._resolve_zone(zone_id)
                # The helper write path merges into the stored zone and keeps a
                # new icon in the registry, where it stays clearable, unless the
                # zone already stores one (#2643). It takes the storage id first.
                result = await _execute_update_simple_helper(
                    self._client,
                    "zone",
                    entity_id or storage_id,
                    storage_id,
                    name,
                    icon,
                    None,
                    None,
                    None,
                    wait,
                    False,
                    fields,
                )
                response = _zone_write_response(result, storage_id, "updated")
                # The reply holds the stored zone plus the registry fields this
                # write set; the entity shows its registry icon over the stored
                # one, as ha_get_zone reports it. An icon this write set or
                # cleared ('') is already in the reply.
                if icon is None and registry_icon:
                    response["zone_data"]["icon"] = registry_icon
                response["updated_fields"] = updated
                return response

            if name is None or latitude is None or longitude is None:
                raise_tool_error(
                    create_validation_error(
                        "name, latitude, and longitude are required when creating a zone.",
                    )
                )
            result = await _execute_create_simple_helper(
                self._client,
                "zone",
                name,
                icon,
                None,
                None,
                None,
                wait,
                False,
                fields,
            )
            return _zone_write_response(result, None, "created")

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Error in ha_set_zone ({operation}, zone_id={zone_id}, name={name}): {e}"
            )
            exception_to_structured_error(
                e,
                context={"zone_id": zone_id, "operation": operation},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify coordinates are valid"
                    if operation == "create"
                    else "Verify zone_id exists using ha_get_zone()",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_remove_zone",
        tags={"Zones"},
        annotations=write_hints(
            "Remove Zone", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="zone", id_param="zone_id")
    @log_tool_usage
    async def ha_remove_zone(
        self,
        zone_id: Annotated[
            str,
            Field(description="Zone ID or entity_id of the zone to remove"),
        ],
        wait: Annotated[
            bool,
            Field(
                description="Wait for the zone to be gone before returning. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Remove a Home Assistant zone.

        EXAMPLES:
        - Remove zone: ha_remove_zone("office")
        - Remove by entity_id: ha_remove_zone("zone.office")

        **WARNING:** Removing a zone used in automations may cause those automations to fail.
        Use ha_get_zone() to find the zone_id for the zone you want to remove.

        **NOTE:** Zones defined in YAML, and the home zone, cannot be removed here.
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                zone_id,
                "zone_id",
                suggestions=["Use ha_get_zone() to find existing zone_ids"],
                context={"operation": "remove_zone"},
            )
            entity_id, storage_id, _ = await self._resolve_zone(zone_id)
            result = await self._client.send_websocket_message(
                {"type": "zone/delete", "zone_id": storage_id}
            )
            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ws_failure_code(result),
                        f"Failed to remove zone {entity_id or storage_id}: "
                        f"{result.get('error', 'Unknown error')}",
                        context={"zone_id": zone_id, "entity_id": entity_id},
                        suggestions=[
                            "Use ha_get_zone() without zone_id to see all available zones",
                        ],
                    )
                )
            response: dict[str, Any] = {
                "success": True,
                "zone_id": storage_id,
                "entity_id": entity_id,
                "message": f"Successfully removed zone: {entity_id or storage_id}",
            }
            if entity_id is None:
                not_waited = ", so the removal was not waited on" if wait else ""
                response["warnings"] = [
                    f"Zone {storage_id} had no entity in the registry{not_waited}."
                ]
                return response
            if not wait:
                return response
            try:
                if not await wait_for_entity_removed(self._client, entity_id):
                    response["warnings"] = [
                        f"Deletion confirmed but {entity_id} is still present "
                        "after the wait window."
                    ]
            except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                response["warnings"] = [
                    f"Deletion confirmed but removal verification failed: {e}"
                ]
            return response

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing zone (zone_id={zone_id}): {e}")
            exception_to_structured_error(
                e,
                context={"zone_id": zone_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify zone_id exists using ha_get_zone()",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def _zone_write_response(
    result: dict[str, Any], storage_id: str | None, verb: str
) -> dict[str, Any]:
    """Reshape the helper write path's response into ha_set_zone's."""
    data = result.get("data") or {}
    response: dict[str, Any] = {
        "success": True,
        "zone_data": data,
        "zone_id": data.get("id", storage_id),
        "entity_id": result.get("entity_id"),
        "message": f"Successfully {verb} zone: {data.get('name', storage_id)}",
    }
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


def register_zone_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant zone configuration tools."""
    register_tool_methods(mcp, ZoneTools(client))
