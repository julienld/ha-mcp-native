"""
Device Registry management tools for Home Assistant.

This module provides tools for managing devices (list, get details, update, remove).

Important: Device renaming does NOT cascade to entities - they are independent registries.
"""

import asyncio
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import LegacyBackendTools, legacy_errors
from ..backends.legacy_devices import LegacyDevices
from ..errors import ErrorCode, create_error_response
from ..runtime.device_records import _build_entity_maps, _get_device_info
from ..runtime.device_tools import Devices, DeviceWriteTools
from ..runtime.devices import DeviceDiscoveryTools
from ..utils.device_registry_semantics import (
    EFFECTIVE_AREA_MARKER,
    annotate_device_rows_with_effective_area,
)
from .auto_backup import with_auto_backup
from .component_devices import (
    fetch_device_list_via_component,
    fetch_device_via_component,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints


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
    """Remove once with Core's current API, retaining the pre-2026.9 fallback."""
    current = await client.send_websocket_message(
        {"type": "config/device_registry/remove", "device_id": device_id}
    )
    if current.get("success") or current.get("error_code") != "unknown_command":
        return [
            {
                "config_entry_id": config_entry_id,
                "success": bool(current.get("success")),
                "error": current.get("error") if not current.get("success") else None,
            }
            for config_entry_id in config_entries
        ]
    # Only Core's explicit unknown-command response proves no removal ran.
    # A timeout, connection loss or other refusal must never replay the write.
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


class RegistryTools(LegacyBackendTools, DeviceDiscoveryTools, LegacyDevices):
    """Device registry tools: get, update, and remove devices."""

    @property
    def devices(self) -> Devices:
        return self

    ha_set_device = tool(
        name="ha_set_device",
        tags={"Device Registry"},
        annotations=write_hints(
            "Set Device", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="device", id_param="device_id")(
            log_tool_usage(legacy_errors(DeviceWriteTools.ha_set_device))
        )
    )
    ha_remove_device = tool(
        name="ha_remove_device",
        tags={"Device Registry"},
        annotations=write_hints(
            "Remove Device", destructive=True, idempotent=True, open_world=False
        ),
    )(
        with_auto_backup(domain="device", id_param="device_id")(
            log_tool_usage(legacy_errors(DeviceWriteTools.ha_remove_device))
        )
    )

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


def register_registry_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register device registry management tools."""
    register_tool_methods(mcp, RegistryTools(client))


__all__ = ["RegistryTools", "register_registry_tools"]
