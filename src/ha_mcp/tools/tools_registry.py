"""
Device Registry management tools for Home Assistant.

This module provides tools for managing devices (list, get details, update, remove).

Important: Device renaming does NOT cascade to entities - they are independent registries.
"""

import asyncio
import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import LegacyBackendTools, legacy_errors
from ..errors import ErrorCode, create_error_response
from ..runtime.device_records import _add_integration_hints as _add_integration_hints
from ..runtime.device_records import _build_entity_maps as _build_entity_maps
from ..runtime.device_records import _classify_integration as _classify_integration
from ..runtime.device_records import _device_config_entries as _device_config_entries
from ..runtime.device_records import _extract_zigbee_info as _extract_zigbee_info
from ..runtime.device_records import _filter_devices as _filter_devices
from ..runtime.device_records import (
    _first_ieee_from_connections as _first_ieee_from_connections,
)
from ..runtime.device_records import _get_device_info as _get_device_info
from ..runtime.device_records import _list_devices_result as _list_devices_result
from ..runtime.device_records import _matches_integration as _matches_integration
from ..runtime.device_records import _process_device_domain as _process_device_domain
from ..runtime.devices import DeviceDiscoveryTools
from ..utils.device_registry_semantics import (
    EFFECTIVE_AREA_MARKER,
    annotate_device_rows_with_effective_area,
)
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION, parse_string_list_param
from .component_devices import (
    fetch_device_list_via_component,
    fetch_device_via_component,
)
from .config_helpers.registry import validate_registry_ids
from .helpers import (
    WHITESPACE_CLEARS_NOTE,
    clearable_value,
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


async def _fetch_entity_rows(client: Any) -> list[dict[str, Any]]:
    """Entity-registry rows for the device<->entity maps; tolerant ([] on failure).

    ``device_list`` carries no per-device entity join (only ``device_get`` does),
    so the full-detail list and the single-device legacy fallback still read the
    whole ``config/entity_registry/list``. Entity-registry failure is non-fatal
    (the caller just loses the entity list / hidden filter), matching the prior
    concurrent-fetch behaviour.
    """
    entity_result = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    return entity_result.get("result", []) if entity_result.get("success") else []


async def _strict_entity_rows(client: Any) -> list[dict[str, Any]]:
    """Full entity registry; raises SERVICE_CALL_FAILED on a failed read.

    The device-id resolver's fallback read. Unlike the tolerant
    :func:`_fetch_entity_rows` (whose callers treat a missing entity list as a
    non-fatal degrade), a failed read here MUST surface as a service failure rather
    than collapse to ``[]`` — otherwise the resolver would misreport a real entity
    as ENTITY_NOT_FOUND when the read itself failed (a double transient failure:
    the targeted ``get`` failed, then this fallback list failed too).
    """
    entity_result = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    if not entity_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to access entity registry: {entity_result.get('error', 'Unknown error')}",
            )
        )
    return list(entity_result.get("result", []))


async def _legacy_device_rows(client: Any) -> list[dict[str, Any]]:
    """Full device registry via the legacy ``config/device_registry/list``.

    Raises SERVICE_CALL_FAILED when the read fails, preserving the prior
    fatal-on-device-registry-failure contract.
    """
    list_result = await client.send_websocket_message(
        {"type": "config/device_registry/list"}
    )
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to access device registry: {list_result.get('error', 'Unknown error')}",
            )
        )
    return list(list_result.get("result", []))


async def _fetch_device_rows(client: Any) -> list[dict[str, Any]]:
    """Full device registry — component ``device_list`` or legacy list.

    Routes through ``ha_mcp_tools/device_list`` when the component serves it (the
    raw ``DeviceEntry`` shape, byte-identical to ``config/device_registry/list``),
    else the legacy WS list. Used for LIST mode, where the whole registry is the
    intended payload.
    """
    result = await fetch_device_list_via_component(client)
    if result is not None:
        return annotate_device_rows_with_effective_area(list(result.get("devices", [])))
    return annotate_device_rows_with_effective_area(await _legacy_device_rows(client))


# HA core replies with this error code (ERR_NOT_FOUND) from
# ``config/entity_registry/get`` for a genuinely unknown entity. Any other failure
# code is a transient / structural WS problem, NOT HA's "unknown entity" verdict.
_ENTITY_NOT_FOUND_CODE = "not_found"


async def _resolve_device_id_via_legacy(client: Any, entity_id: str) -> str:
    """Resolve entity_id → device_id via the full ``config/entity_registry/list``.

    The whole-registry scan :func:`_resolve_device_id_for_entity` replaced, kept as
    the fallback for a transient failure of the targeted read so a real entity is
    never misreported as nonexistent. Uses the STRICT list read so a failure of
    THIS fallback surfaces as a service failure rather than an empty list that would
    itself misreport the entity as ENTITY_NOT_FOUND. Raises ENTITY_NOT_FOUND only
    when the read succeeds and the entity is absent or carries no device (the same
    contract).
    """
    entity_to_device, _ = _build_entity_maps(
        await _strict_entity_rows(client), need_full=False
    )
    device_id = entity_to_device.get(entity_id)
    if device_id:
        return device_id
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"Entity '{entity_id}' not found or has no associated device",
            suggestions=["Use ha_search() to find valid entity IDs"],
            context={"entity_id": entity_id},
        )
    )
    raise AssertionError  # unreachable: raise_tool_error is NoReturn


async def _resolve_device_id_for_entity(client: Any, entity_id: str) -> str:
    """Resolve an entity_id to its device_id via a single native entity read.

    Uses HA's targeted ``config/entity_registry/get`` (one entity, no
    whole-registry dump) rather than scanning the full entity registry. HA returns
    ``success: false`` both for a genuinely unknown entity (error code
    ``not_found``) AND for a transient failure, so the two are told apart by the
    structured error code: ``not_found`` (or a successful read with no device)
    raises ENTITY_NOT_FOUND — the same contract the old full-map lookup produced —
    while any OTHER failure falls back to the legacy whole-registry read so a WS
    hiccup never misreports a real entity as nonexistent.
    """
    result = await client.send_websocket_message(
        {"type": "config/entity_registry/get", "entity_id": entity_id}
    )
    if result.get("success"):
        device_id = (result.get("result") or {}).get("device_id")
        if device_id:
            return str(device_id)
    elif result.get("error_code") != _ENTITY_NOT_FOUND_CODE:
        return await _resolve_device_id_via_legacy(client, entity_id)
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"Entity '{entity_id}' not found or has no associated device",
            suggestions=["Use ha_search() to find valid entity IDs"],
            context={"entity_id": entity_id},
        )
    )
    raise AssertionError  # unreachable: raise_tool_error is NoReturn


async def _single_device_and_entities(
    client: Any, device_id: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """(``all_devices``, ``device_to_entities``) for a single-device lookup.

    When the component serves ``device_get`` and the device exists, returns
    ``[device]`` plus its entity map — both from ONE ``device_get(include_entities)``
    frame, so neither the device NOR the entity registry is dumped. Falls back to
    the legacy full device + entity registries (so a not-found error can still
    suggest valid ids and the device's entities are still listed) when the device
    is absent (component said ``None``), the component can't serve device_get, or
    the component served the device but not the additive entities half (no
    ``entities`` key — an older device_get predating ``include_entities``): the
    join's absence degrades to legacy rather than reporting a device with zero
    entities.
    """
    result = await fetch_device_via_component(client, device_id, include_entities=True)
    if result is not None:
        device = result.get("device")
        entities = result.get("entities")
        if isinstance(device, dict) and isinstance(entities, list):
            device = dict(device)
            if "effective_area_id" in result:
                device[EFFECTIVE_AREA_MARKER] = result.get("effective_area_id")
            _, device_to_entities = _build_entity_maps(entities, need_full=True)
            return [device], device_to_entities
        # Either an authoritative not-found (device is None) — fall through so the
        # error carries available_device_ids — or a component that served
        # device_get without the entities half. Both take the legacy device AND
        # entity registries.
    # Device registry first, then entity registry — the legacy wire order,
    # which the #1297 error-contract test pins with an ordered mock.
    all_devices = annotate_device_rows_with_effective_area(
        await _legacy_device_rows(client)
    )
    all_entities = await _fetch_entity_rows(client)
    _, device_to_entities = _build_entity_maps(all_entities, need_full=True)
    return all_devices, device_to_entities


async def _lookup_device_for_remove(client: Any, device_id: str) -> dict[str, Any]:
    """Return the raw device row for ``ha_remove_device``; raise if it does not exist.

    Uses the component's ``device_get`` for the single-device read when available
    (no whole-registry dump), falling back to the legacy full list — also used to
    build the not-found error's available-ids suggestion.
    """
    result = await fetch_device_via_component(client, device_id)
    if result is not None:
        device = result.get("device")
        if isinstance(device, dict):
            return device
    all_rows = await _legacy_device_rows(client)
    device = next((d for d in all_rows if d.get("id") == device_id), None)
    if device is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Device not found: {device_id}",
                suggestions=["Use ha_get_device() to find valid device IDs"],
                context={
                    "device_id": device_id,
                    "available_device_ids": [
                        d.get("id") for d in all_rows[:10] if d.get("id")
                    ],
                },
            )
        )
        return {}  # unreachable: raise_tool_error always raises
    return device


@legacy_errors
async def _get_single_device_result(
    client: Any,
    device_id: str,
    entity_id: str | None,
    all_devices: list[dict[str, Any]],
    device_to_entities: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return await RegistryTools(client)._get_single_device_result(
        device_id, entity_id, all_devices, device_to_entities
    )


async def _remove_device_config_entries(
    client: Any,
    device_id: str,
    config_entries: list[str],
) -> list[dict[str, Any]]:
    """Remove device from each config entry concurrently; returns per-entry results."""
    raw = await asyncio.gather(
        *(
            client.send_websocket_message(
                {
                    "type": "config/device_registry/remove_config_entry",
                    "device_id": device_id,
                    "config_entry_id": config_entry_id,
                }
            )
            for config_entry_id in config_entries
        ),
        return_exceptions=True,
    )
    results: list[dict[str, Any]] = []
    for config_entry_id, r in zip(config_entries, raw, strict=True):
        if isinstance(r, BaseException):
            results.append(
                {"config_entry_id": config_entry_id, "success": False, "error": str(r)}
            )
        else:
            results.append(
                {
                    "config_entry_id": config_entry_id,
                    "success": r.get("success", False),
                    "error": r.get("error") if not r.get("success") else None,
                }
            )
    return results


class RegistryTools(LegacyBackendTools, DeviceDiscoveryTools):
    """Device registry tools: get, update, and remove devices."""

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
            message: dict[str, Any] = {
                "type": "config/device_registry/update",
                "device_id": device_id,
            }

            updates_made = []

            if name is not None:
                name = clearable_value(name, "name")
                message["name_by_user"] = name
                updates_made.append(f"name='{name}'" if name else "name cleared")

            if area_id is not None:
                area_id = clearable_value(area_id, "area_id")
                message["area_id"] = area_id
                updates_made.append(
                    f"area_id='{area_id}'" if area_id else "area cleared"
                )

            if disabled_by is not None:
                disabled_by = clearable_value(
                    disabled_by,
                    "disabled_by",
                    hint="Pass 'user' to disable the device, or '' (a single space "
                    "' ' if your client cannot send an empty string) to enable it",
                )
                message["disabled_by"] = disabled_by
                updates_made.append(
                    f"disabled_by='{disabled_by}'" if disabled_by else "enabled"
                )

            if labels is not None:
                message["labels"] = labels
                updates_made.append(f"labels={labels}")

            if not updates_made:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "No updates specified",
                        suggestions=[
                            "Provide at least one of: name, area_id, disabled_by, or labels",
                        ],
                        context={"device_id": device_id},
                    )
                )

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

    @staticmethod
    def _get_device_info(device: dict[str, Any]) -> dict[str, Any]:
        return _get_device_info(device)

    def _raise_device_error(self, error: Exception) -> NoReturn:
        if isinstance(error, ToolError):
            raise error
        exception_to_structured_error(error)

    async def _resolve_device_id_for_entity(self, entity_id: str) -> str:
        return await _resolve_device_id_for_entity(self._client, entity_id)

    async def _single_device_and_entities(
        self, device_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        return await _single_device_and_entities(self._client, device_id)

    async def _list_device_snapshot(
        self, need_full: bool
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        all_entities = await _fetch_entity_rows(self._client) if need_full else []
        _, device_to_entities = _build_entity_maps(all_entities, need_full)
        all_devices = await _fetch_device_rows(self._client)
        return all_devices, device_to_entities

    ha_get_device = tool(
        name="ha_get_device",
        tags={"Device Registry", "Zigbee", "Z-Wave", "Matter"},
        annotations=read_only_hints(
            "Get Device (incl. Zigbee/ZHA/Z2M, Z-Wave and Matter)", open_world=False
        ),
    )(log_tool_usage(legacy_errors(DeviceDiscoveryTools.ha_get_device)))

    @tool(
        name="ha_set_device",
        tags={"Device Registry"},
        annotations=write_hints(
            "Set Device", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="device", id_param="device_id")
    @log_tool_usage
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

    @tool(
        name="ha_remove_device",
        tags={"Device Registry"},
        annotations=write_hints(
            "Remove Device", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="device", id_param="device_id")
    @log_tool_usage
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


def register_registry_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register device registry management tools."""
    register_tool_methods(mcp, RegistryTools(client))
