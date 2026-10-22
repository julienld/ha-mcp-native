"""Builders for the config/entity_registry/update message of ha_set_entity."""

from collections.abc import Awaitable, Callable
from typing import Any

from packaging.version import InvalidVersion, Version

from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import entity_fields as _fields

from ..errors import ErrorCode, create_error_response
from ..runtime.entity_records import uses_device_name as uses_device_name
from .helpers import raise_tool_error

# Home Assistant release that reads an empty entity-name override as "use the
# device name" (home-assistant/core#181576). Older Cores store "" but render
# the default name, so the switch would silently do the opposite there.
USE_DEVICE_NAME_MIN_CORE = (2026, 10)


async def ensure_use_device_name_allowed(
    use_device_name: bool | None,
    client: Any,
    entity_id: str,
    fetch_entity: Callable[[str], Awaitable[dict[str, Any]]],
) -> None:
    """use_device_name=True needs a 2026.10 Core and an entity with a device."""
    if not use_device_name:
        return
    await ensure_use_device_name_supported(client)
    try:
        current = await fetch_entity(entity_id)
    except ValueError as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Entity not found: {e}",
                context={"entity_id": entity_id},
                suggestions=["Use ha_search() to find valid entity IDs"],
            )
        )
    reject_use_device_name_without_device(entity_id, current)


async def ensure_use_device_name_supported(client: Any) -> None:
    """Refuse use_device_name=True on a Core older than 2026.10."""
    config = await client.get_config()
    raw = str(config.get("version", ""))
    try:
        release = Version(raw).release[:2]
    except InvalidVersion:
        release = None
    if release is not None and tuple(release) >= USE_DEVICE_NAME_MIN_CORE:
        return
    detail = (
        f"this Core reports {raw} and would show the default name instead"
        if release is not None
        else f"this Core's version {raw!r} could not be parsed, so support "
        "cannot be confirmed"
    )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"use_device_name needs Home Assistant 2026.10 or newer; {detail}",
            context={"ha_version": raw},
            suggestions=[
                "Upgrade Home Assistant to 2026.10 or newer",
                "Pass name='<device name>' to set the device's name as a fixed custom name",
            ],
        )
    )


build_name_visibility_fields = legacy_errors_sync(_fields.build_name_visibility_fields)
build_state_tag_fields = legacy_errors_sync(_fields.build_state_tag_fields)
reject_name_with_use_device_name = legacy_errors_sync(
    _fields.reject_name_with_use_device_name
)
reject_use_device_name_without_device = legacy_errors_sync(
    _fields.reject_use_device_name_without_device
)
