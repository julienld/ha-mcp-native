"""Create path for simple (non-flow) helper types."""

from typing import Any

from ...backends.legacy import legacy_errors
from ...utils.registry_update_lock import registry_update_lock
from ..config_write_helpers import apply_entity_category
from .core_payload import (
    check_core_gaps,
    with_create_defaults,
)
from .registry import _ws_error_msg


def _build_create_message(
    helper_type: str, name: str, icon: str | None, fields: dict[str, Any]
) -> dict[str, Any]:
    """The WebSocket {type}/create message: the caller's fields under Core's names."""
    message: dict[str, Any] = {"name": name}
    if icon:
        message["icon"] = icon
    message.update(with_create_defaults(helper_type, fields))
    check_core_gaps(helper_type, message)
    message["type"] = f"{helper_type}/create"  # never a field's
    return message


async def _apply_create_entity_registry(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    helper_data: dict[str, Any],
    warnings: list[str],
    registry_icon: str | None = None,
) -> None:
    """Write area, labels and ``registry_icon`` to the entity registry after a
    create; echo icon, area and labels into helper_data once it succeeds."""
    if area_id is None and labels is None and not registry_icon:
        return
    update_message: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if registry_icon:
        update_message["icon"] = registry_icon
    if area_id is not None:
        update_message["area_id"] = area_id if area_id else None
    if labels is not None:
        update_message["labels"] = labels
    async with registry_update_lock("entity", entity_id):
        update_result = await client.send_websocket_message(update_message)
    if update_result.get("success"):
        if icon is not None:
            helper_data["icon"] = icon if icon else None
        if area_id is not None:
            helper_data["area_id"] = area_id if area_id else None
        if labels is not None:
            helper_data["labels"] = labels
    else:
        warnings.append(
            f"Helper created but entity registry update failed: {_ws_error_msg(update_result)}"
        )


async def _apply_create_category(
    client: Any,
    entity_id: str,
    category: str | None,
    helper_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply category to a newly created helper entity."""
    if not (category and entity_id):
        return
    cat_result: dict[str, Any] = {}
    await apply_entity_category(
        client, entity_id, category, "helpers", cat_result, "helper"
    )
    if "category" in cat_result:
        helper_data["category"] = cat_result["category"]
    elif cat_result.get("warnings"):
        warnings.extend(cat_result["warnings"])


@legacy_errors
async def _execute_create_simple_helper(
    client: Any,
    helper_type: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    wait: bool,
    MandatoryBPS: bool,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility call into the shared storage-helper implementation."""
    from ...backends.legacy_helper_storage import storage_tools

    tools = storage_tools(client)
    async with tools._schema_context(helper_type, "create"):
        return await tools._create_simple(
            helper_type,
            name,
            icon,
            area_id,
            labels,
            category,
            wait,
            MandatoryBPS,
            fields,
        )
