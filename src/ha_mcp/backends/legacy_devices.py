"""Qualified remote device writes, retained for standalone compatibility."""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.device_records import _device_config_entries
from ha_mcp.runtime.device_values import device_update_values as _device_update_values
from ha_mcp.tools.coercion import parse_string_list_param
from ha_mcp.tools.config_helpers.registry import validate_registry_ids
from ha_mcp.tools.helpers import (
    exception_to_structured_error,
    raise_tool_error,
    validate_identifier_not_empty,
)

from .legacy import legacy_errors_sync

logger = logging.getLogger(__name__)
device_update_values = legacy_errors_sync(_device_update_values)


class LegacyDevices:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def _update_device_internal(
        self,
        device_id: str,
        name: str | None = None,
        area_id: str | None = None,
        disabled_by: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Internal implementation of device update."""
        try:
            payload, updates_made = device_update_values(
                device_id,
                name,
                area_id,
                disabled_by,
                labels,
            )
            message = {"type": "config/device_registry/update", **payload}
            area_id = payload.get("area_id")

            # Issue #2159: HA stores an unknown area_id or label_id on the
            # device verbatim. Device labels replace the existing set, so the
            # supplied list is the full set to check; an empty list clears.
            await validate_registry_ids(
                self._client, area_id, labels, None, fail_closed=True
            )

            logger.info(f"Updating device {device_id}: {', '.join(updates_made)}")
            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                device_entry = result.get("result", {})
                return {
                    "success": True,
                    "device_id": device_id,
                    "updates": updates_made,
                    "device_entry": {
                        "name": device_entry.get("name_by_user")
                        or device_entry.get("name"),
                        "name_by_user": device_entry.get("name_by_user"),
                        "area_id": device_entry.get("area_id"),
                        "disabled_by": device_entry.get("disabled_by"),
                        "labels": device_entry.get("labels", []),
                    },
                    "message": f"Device updated: {', '.join(updates_made)}",
                    "note": "Remember: Device rename does NOT cascade to entities. Use ha_set_entity(new_entity_id=...) to rename entities.",
                }
            else:
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to update device: {error_msg}",
                        suggestions=[
                            "Verify the device_id exists using ha_get_device()",
                            "Check that area_id exists if specified",
                        ],
                        context={"device_id": device_id},
                    )
                )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error updating device: {e}")
            exception_to_structured_error(
                e,
                context={"device_id": device_id},
            )
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def set(
        self,
        device_id: str,
        name: str | None = None,
        area_id: str | None = None,
        disabled_by: str | None = None,
        labels: str | list[str] | None = None,
    ) -> dict[str, Any]:
        parsed_labels = None
        if labels is not None:
            try:
                parsed_labels = parse_string_list_param(labels, "labels")
            except ValueError as e:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid labels parameter: {e}",
                    )
                )

        # Empty/whitespace device_id would reach the
        # ``config/device_registry/update`` WS message inside
        # ``_update_device_internal`` and surface as a misleading HA
        # "device not found" — same destructive-WS-call class as the
        # ``ha_remove_device`` guard added in this PR.
        validate_identifier_not_empty(
            device_id,
            "device_id",
            suggestions=["Use ha_get_device() to find valid device IDs"],
        )
        return await self._update_device_internal(
            device_id=device_id,
            name=name,
            area_id=area_id,
            disabled_by=disabled_by,
            labels=parsed_labels,
        )

    async def remove(self, device_id: str) -> dict[str, Any]:
        from ha_mcp.tools.tools_registry import (
            _lookup_device_for_remove,
            _remove_device_config_entries,
        )

        try:
            # Empty/whitespace device_id would slip past the local-filter dict
            # lookup below and surface as a generic "Device not found: " error
            # after a wasted registry-list round-trip.
            validate_identifier_not_empty(
                device_id,
                "device_id",
                suggestions=["Use ha_get_device() to find valid device IDs"],
            )
            device = await _lookup_device_for_remove(self._client, device_id)

            config_entries = _device_config_entries(device)
            device_name = device.get("name_by_user") or device.get("name")
            if not config_entries:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Device has no config entries - cannot be removed via this method",
                        suggestions=[
                            "This device may be managed by an integration directly. Try disabling it instead.",
                        ],
                        context={"device_id": device_id, "device_name": device_name},
                    )
                )

            removal_results = await _remove_device_config_entries(
                self._client, device_id, config_entries
            )
            all_succeeded = all(r["success"] for r in removal_results)
            any_succeeded = any(r["success"] for r in removal_results)

            if all_succeeded:
                return {
                    "success": True,
                    "device_id": device_id,
                    "device_name": device_name,
                    "config_entries_removed": len(config_entries),
                    "message": f"Successfully removed device from {len(config_entries)} config entry/entries",
                }
            elif any_succeeded:
                return {
                    "success": True,
                    "partial": True,
                    "device_id": device_id,
                    "device_name": device_name,
                    "removal_results": removal_results,
                    "message": "Device partially removed - some config entries could not be removed",
                }
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Failed to remove device from any config entries",
                        suggestions=[
                            "Device may be actively managed by its integration. Try disabling it instead.",
                        ],
                        context={
                            "device_id": device_id,
                            "removal_results": removal_results,
                        },
                    )
                )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing device: {e}")
            exception_to_structured_error(
                e,
                context={"device_id": device_id},
            )
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable
