"""Shared zone read tool over authoritative collection/state snapshots."""

from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure

from .errors import raise_failure
from .zone_records import render_zone_snapshot


class ZoneReadTools(BackendTools):
    async def _zone_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = await self.backend.execute(
            "zones.snapshot", {}, self.context
        )
        return snapshot

    def _map_zone_error(self, error: Exception, zone_id: str | None) -> NoReturn:
        raise_failure(
            create_error_response(
                ErrorCode.INTERNAL_ERROR, str(error), context={"zone_id": zone_id}
            )
        )

    async def ha_get_zone(
        self,
        zone_id: Annotated[
            str | None,
            Field(
                description="Zone ID or entity_id to get details for (from the ha_get_zone() list).",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get zone information - list all zones or get details for a specific one.

        Without a zone_id: Lists all Home Assistant zones with their coordinates and radius.
        With a zone_id: Returns detailed configuration for a specific zone.

        EXAMPLES:
        - List all zones: ha_get_zone()
        - Get specific zone: ha_get_zone(zone_id="office")
        - Same zone by entity_id: ha_get_zone(zone_id="zone.office")

        Zones outside the zone storage, the home zone (from the general
        settings) and YAML zones, are listed with ``editable=false`` /
        ``source="yaml"``; editable means ha_set_zone and ha_remove_zone can
        change the zone. Each zone carries its entity_id.
        """
        try:
            return render_zone_snapshot(await self._zone_snapshot(), zone_id)
        except ToolFailure:
            raise
        except Exception as error:  # noqa: BLE001 - facade-specific error classification
            self._map_zone_error(error, zone_id)
