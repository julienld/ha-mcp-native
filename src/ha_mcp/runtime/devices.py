"""Shared complete device discovery, including optional protocol diagnostics."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure

from .device_records import (
    _device_config_entries,
    _get_device_info,
    _list_devices_result,
)
from .errors import raise_failure
from .radio import enrich_matter_diagnostics, enrich_zha_metrics, enrich_zwave_status


class DeviceDiscoveryTools(BackendTools):
    _get_device_info = staticmethod(_get_device_info)

    def _raise_device_error(self, error: Exception) -> NoReturn:
        raise_failure(create_error_response(ErrorCode.INTERNAL_ERROR, str(error)))

    async def _resolve_device_id_for_entity(self, entity_id: str) -> str:
        device_id = await self.backend.execute(
            "devices.resolve", {"entity_id": entity_id}, self.context
        )
        if device_id:
            return str(device_id)
        return raise_failure(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Entity '{entity_id}' not found or has no associated device",
                suggestions=["Use ha_search() to find valid entity IDs"],
                context={"entity_id": entity_id},
            )
        )

    async def _single_device_and_entities(
        self, device_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        result = await self.backend.execute(
            "devices.single_snapshot", {"device_id": device_id}, self.context
        )
        return result["devices"], result["device_to_entities"]

    async def _list_device_snapshot(
        self, need_full: bool
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        result = await self.backend.execute(
            "devices.list_snapshot", {"include_entities": need_full}, self.context
        )
        return result["devices"], result["device_to_entities"]

    async def _get_single_device_result(
        self,
        device_id: str,
        entity_id: str | None,
        all_devices: list[dict[str, Any]],
        device_to_entities: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Return the full-detail single-device response dict."""
        device = next((d for d in all_devices if d.get("id") == device_id), None)
        if not device:
            raise_failure(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Device not found: {device_id}",
                    suggestions=["Use ha_get_device() to find valid device IDs"],
                    context={
                        "device_id": device_id,
                        "available_device_ids": [
                            d.get("id") for d in all_devices[:10] if d.get("id")
                        ],
                    },
                )
            )
            return {}
        enrichment_warnings: list[str] = []
        device_info = self._get_device_info(device)
        device_info["entities"] = device_to_entities.get(device_id, [])
        device_info["name_by_user"] = device.get("name_by_user")
        device_info["default_name"] = device.get("name")
        device_info["hw_version"] = device.get("hw_version")
        device_info["serial_number"] = device.get("serial_number")
        device_info["disabled_by"] = device.get("disabled_by")
        device_info["labels"] = device.get("labels", [])
        device_info["config_entries"] = _device_config_entries(device)
        device_info["connections"] = device.get("connections", [])
        device_info["identifiers"] = device.get("identifiers", [])
        if device_info.get("integration_type") == "zha" and device_info.get(
            "ieee_address"
        ):
            await enrich_zha_metrics(
                self.backend, self.context, device_info, enrichment_warnings
            )
        if device_info.get("integration_type") == "zwave_js" and device_info.get(
            "node_id"
        ):
            await enrich_zwave_status(
                self.backend, self.context, device_id, device_info, enrichment_warnings
            )
        if device_info.get("integration_type") == "matter":
            await enrich_matter_diagnostics(
                self.backend, self.context, device_id, device_info, enrichment_warnings
            )
        entities = device_info.get("entities", [])
        response: dict[str, Any] = {
            "success": True,
            "device": device_info,
            "entities": entities,
            "entity_count": len(entities),
            "queried_by": "entity_id" if entity_id else "device_id",
            "queried_entity_id": entity_id,
        }
        if enrichment_warnings:
            response["warnings"] = enrichment_warnings
        return response

    async def ha_get_device(
        self,
        device_id: Annotated[
            str | None,
            Field(description="Device ID to retrieve details for.", default=None),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Entity ID to find the associated device for (e.g., 'light.living_room')",
                default=None,
            ),
        ] = None,
        integration: Annotated[
            str | None,
            Field(
                description="Filter devices by integration: 'zha', 'zigbee2mqtt', 'zwave_js', 'mqtt', 'hue', etc.",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Filter devices by area ID (e.g., 'living_room')",
                default=None,
            ),
        ] = None,
        manufacturer: Annotated[
            str | None,
            Field(
                description="Filter devices by manufacturer name (e.g., 'Philips')",
                default=None,
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=50,
                ge=1,
                le=200,
                description="Max devices to return per page in list mode",
            ),
        ] = 50,
        offset: Annotated[
            int,
            Field(
                default=0, ge=0, description="Number of devices to skip for pagination"
            ),
        ] = 0,
        detail_level: Annotated[
            Literal["summary", "full"],
            Field(
                default="summary",
                description="'summary': basic device info and protocol identifiers. 'full': in list mode also include each device's entities. Single-device lookups always return full detail, including radio metrics, node status and Matter diagnostics.",
            ),
        ] = "summary",
    ) -> dict[str, Any]:
        """Get device information with pagination, including Zigbee (ZHA/Z2M) and Z-Wave JS devices.

        Without device_id/entity_id: Lists devices with optional filters and pagination.
        With device_id or entity_id: Returns full detail for that specific device.

        **List devices (paginated):**
        - First page: ha_get_device()
        - Next page: ha_get_device(offset=50)
        - By area: ha_get_device(area_id="living_room")
        - By integration: ha_get_device(integration="zigbee2mqtt")
        - Full details in list: ha_get_device(detail_level="full", limit=10)

        **Single device lookup (always full detail):**
        - By device_id: ha_get_device(device_id="abc123")
        - By entity_id: ha_get_device(entity_id="light.living_room")

        **Zigbee:** integration="zha" or "zigbee2mqtt". Returns ieee_address, radio metrics.
        **Z-Wave:** integration="zwave_js". Returns node_id, node_status.
        **Matter:** integration="matter". Returns node_diagnostics (network type,
        reachability, IPs, fabrics). For management use ha_manage_radio.
        """
        try:
            if entity_id and (not device_id):
                device_id = await self._resolve_device_id_for_entity(entity_id)
            if device_id:
                (
                    all_devices,
                    device_to_entities,
                ) = await self._single_device_and_entities(device_id)
                return await self._get_single_device_result(
                    device_id, entity_id, all_devices, device_to_entities
                )
            all_devices, device_to_entities = await self._list_device_snapshot(
                detail_level == "full"
            )
            return _list_devices_result(
                all_devices,
                device_to_entities,
                integration,
                area_id,
                manufacturer,
                limit,
                offset,
                detail_level,
            )
        except ToolFailure:
            raise
        except Exception as e:  # noqa: BLE001 - facade-specific error classification
            self._raise_device_error(e)
