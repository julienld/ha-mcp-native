"""Shared public system-health signature, independent of Core and MCP."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class HealthFacade:
    def __init__(self, get_health: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.get_health = get_health

    async def ha_get_system_health(
        self,
        include: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated extra sections: 'repairs' (Repair items, "
                    "active only unless include_dismissed_repairs=True); "
                    "'zha_network' (ZHA devices with radio signal summary: name, "
                    "LQI, RSSI); 'zha_network_full' (all ZHA device details; large "
                    "on 100+ device networks); 'zwave_network' (Z-Wave JS status "
                    "and node summary: status, security, routing); "
                    "'thread_network' (per border-router channel, extended_pan_id "
                    "and border_agent_id; not per-node Thread health); "
                    "'matter_network' (Matter integration presence: "
                    "config_entry_id, state, title; per-node health is in Matter "
                    "node diagnostics); 'themes' (installed theme names, count, "
                    "default_theme, default_dark_theme); 'diagnostics' "
                    "(per-integration diagnostics dump, integration-defined JSON; "
                    "REQUIRES config_entry_id; payloads can be large — pair with "
                    "diagnostics_fields or diagnostics_truncate_at_bytes); "
                    "'config_check' (validate the HA configuration, the "
                    "pre-restart check ha_restart runs automatically; returns "
                    "{result: valid|invalid, is_valid, errors}); 'dead_entities' "
                    "(orphaned/stale entity-registry entries: config_entry_orphans "
                    "whose owning integration instance is gone, and stale_restored "
                    "entries HA restored on startup that the loaded integration no "
                    "longer provides, each with entity_id + platform for cleanup "
                    "via ha_remove_entity; unknown-state entities and "
                    "merely-offline devices are excluded)."
                )
            ),
        ] = None,
        include_dismissed_repairs: Annotated[
            bool | None,
            Field(
                description=(
                    "Include user-dismissed/ignored repairs. Only meaningful when "
                    "'repairs' is in include. Dismiss or restore one with "
                    "ha_manage_updates(action='ignore_repair' / 'unignore_repair')."
                )
            ),
        ] = False,
        config_entry_id: Annotated[
            str | None,
            Field(
                description="Config entry ID of the integration (find via ha_get_integration). Required when include contains 'diagnostics'."
            ),
        ] = None,
        device_id: Annotated[
            str | None,
            Field(
                description="With include='diagnostics', return the device-scoped dump for this device instead of the full integration dump. Some integrations only expose config-entry-level dumps."
            ),
        ] = None,
        diagnostics_fields: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(
                description="Top-level keys to keep from the diagnostics data payload (e.g. ['home_assistant', 'issues']). Accepts a JSON list or comma-separated string. Only applies with include='diagnostics'."
            ),
        ] = None,
        diagnostics_truncate_at_bytes: Annotated[
            int | None,
            Field(
                ge=1,
                description="Byte cap on the serialized diagnostics payload (post-projection / post-data_path). On hit, drops data and emits truncated=true, bytes_total, byte_cap, plus available_fields (when the capped value is a dict). Recommended starting point: 20000. Only applies with include='diagnostics'.",
            ),
        ] = None,
        diagnostics_data_path: Annotated[
            str | None,
            Field(
                description="Dotted path into the diagnostics data sub-tree (e.g. 'data.devices' for ZHA per-device records). Walks into the post-fields payload. Resolution failures replace data with null and surface data_path_error. Only applies with include='diagnostics'."
            ),
        ] = None,
        diagnostics_data_offset: Annotated[
            int | None,
            Field(
                ge=0,
                description="Pagination start index for list-valued diagnostics_data_path results. Only applies with include='diagnostics'.",
            ),
        ] = 0,
        diagnostics_data_limit: Annotated[
            int | None,
            Field(
                ge=1,
                description="Pagination window for list-valued diagnostics_data_path results; data becomes {path, items, offset, limit, total, has_more}. Only applies with include='diagnostics'.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get Home Assistant system health, including Zigbee (ZHA), Z-Wave JS, and per-integration diagnostics dumps.

        Returns health check results from integrations, system resources, and
        connectivity; available information varies by installation type and
        loaded integrations. Optional sections are selected with `include`.

        The result may include ``ha_mcp_update`` ``{current, latest,
        update_available}``. When update_available is true, tell the user a
        newer ha-mcp release is available.

        EXAMPLES:
        - ha_get_system_health(include="repairs,zha_network,zwave_network,config_check")
        - Page a list-valued diagnostics sub-tree: ha_get_system_health(include="diagnostics", config_entry_id="abc", diagnostics_data_path="<list-valued path>", diagnostics_data_limit=10), then repeat with diagnostics_data_offset=10 while has_more is true.
        """
        return await self.get_health(
            include=include,
            include_dismissed_repairs=include_dismissed_repairs,
            config_entry_id=config_entry_id,
            device_id=device_id,
            diagnostics_fields=diagnostics_fields,
            diagnostics_truncate_at_bytes=diagnostics_truncate_at_bytes,
            diagnostics_data_path=diagnostics_data_path,
            diagnostics_data_offset=diagnostics_data_offset,
            diagnostics_data_limit=diagnostics_data_limit,
        )
