"""One device-write contract bound to a canonical or compatibility service."""

from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.runtime.validation import WHITESPACE_CLEARS_NOTE
from ha_mcp.tools.coercion import JSON_STRING_COERCION


class Devices(Protocol):
    async def set(
        self,
        device_id: str,
        name: str | None = None,
        area_id: str | None = None,
        disabled_by: str | None = None,
        labels: str | list[str] | None = None,
    ) -> dict[str, Any]: ...
    async def remove(self, device_id: str) -> dict[str, Any]: ...


class DeviceWriteTools:
    def __init__(self, devices: Devices) -> None:
        self.devices = devices

    async def ha_set_device(
        self,
        device_id: Annotated[
            str,
            Field(description="Device ID to update"),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "New display name for the device (sets name_by_user). '' clears "
                    "name_by_user and reverts to the integration-provided name. "
                    + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description=(
                    "Area/room ID to assign the device to. Use empty string '' to "
                    "unassign. " + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        disabled_by: Annotated[
            str | None,
            Field(
                description=(
                    "Set to 'user' to disable the device, or '' (empty string) "
                    "to enable it. Omit to leave the disabled state unchanged. "
                    + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Labels to assign to the device (replaces existing labels)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update device properties such as name, area, disabled state, or labels.

        IMPORTANT: Renaming a device does NOT rename its entities!
        Device and entity names are independent. To rename entities, use ha_set_entity(new_entity_id=...).

        Common workflow for full rename:
        1. ha_set_device(device_id="abc", name="Living Room Sensor")  # Rename device
        2. ha_set_entity("sensor.old", new_entity_id="sensor.living_room")  # Rename entities separately

        EXAMPLES:
        - Rename device: ha_set_device("abc123", name="Living Room Hub")
        - Move to area: ha_set_device("abc123", area_id="living_room")
        - Disable device: ha_set_device("abc123", disabled_by="user")
        - Enable device: ha_set_device("abc123", disabled_by="")
        - Add labels: ha_set_device("abc123", labels=["important", "sensor"])
        """
        return await self.devices.set(
            device_id=device_id,
            name=name,
            area_id=area_id,
            disabled_by=disabled_by,
            labels=labels,
        )

    async def ha_remove_device(
        self,
        device_id: Annotated[
            str,
            Field(description="Device ID to remove from the registry"),
        ],
    ) -> dict[str, Any]:
        """
        Remove an orphaned device from the Home Assistant device registry.

        WARNING: This removes the device entry from the registry.
        - Use only for orphaned devices that are no longer connected
        - Active devices will typically be re-added by their integration
        - Associated entities may also be removed

        Works only when the integration supports removing devices.

        EXAMPLES:
        - Remove orphaned device: ha_remove_device("abc123def456")

        NOTE: For most use cases, consider disabling the device instead:
        ha_set_device(device_id="abc123", disabled_by="user")
        """
        return await self.devices.remove(device_id)
