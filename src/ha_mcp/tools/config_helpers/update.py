"""Update path for simple (non-flow) helper types.

The input_*, counter and timer collections replace the whole item on update
(zone, person and tag merge into it), so every update is the stored item plus
the caller's changes (:func:`merged_update`), validated by Core itself. The
component writes it in-process; without the component the item is read from
``<type>/list`` and sent to ``<type>/update``.
"""

from typing import Any, cast

from ...backends.legacy import legacy_errors
from ...errors import ErrorCode, create_error_response
from ...utils.registry_update_lock import registry_update_lock
from ..config_write_helpers import apply_entity_category
from ..helpers import raise_tool_error
from .listing import listed_items
from .registry import _ws_error_msg
from .schemas import (
    _simple_helper_error_context,
)


async def _stored_item(
    client: Any, helper_type: str, entity_id: str, unique_id: str
) -> dict[str, Any]:
    """The stored item from ``<type>/list`` (person nests its storage items)."""
    list_result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to fetch {helper_type} config list: "
                f"{list_result.get('error', 'Unknown')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    items = listed_items(list_result.get("result"))
    existing = next(
        (i for i in items if isinstance(i, dict) and i.get("id") == unique_id), None
    )
    if not existing:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"{helper_type} config not found for id: {unique_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return existing


async def _execute_legacy_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    unique_id: str,
    name: str | None,
    icon: str | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility result shape over the shared update operation."""
    response = await _execute_update_simple_helper(
        client,
        helper_type,
        entity_id,
        unique_id,
        name,
        icon,
        None,
        None,
        None,
        False,
        False,
        fields,
    )
    return cast(dict[str, Any], response.get("data", {}))


async def _resolve_update_unique_id(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
    name: str | None,
) -> str:
    """Look up the unique_id for a helper entity via the entity registry."""
    registry_result = await client.send_websocket_message(
        {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
    )
    if not registry_result.get("success"):
        suggestions = [
            f"Verify the helper_id={helper_id!r} exists "
            "(use ha_config_list_helpers to list current helpers)",
        ]
        if name:
            suggestions.append(
                f"If you meant to create a new helper named {name!r}, "
                "omit helper_id (or pass action='create')"
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Could not find {helper_type} entity: {entity_id}",
                context=_simple_helper_error_context(
                    helper_type, entity_id=entity_id, helper_id=helper_id, name=name
                ),
                suggestions=suggestions,
            )
        )
    registry_entry = registry_result.get("result", {})
    if not isinstance(registry_entry, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Unexpected registry response for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    unique_id = registry_entry.get("unique_id")
    if not unique_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"No unique_id found in entity registry for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return unique_id  # type: ignore[no-any-return]


async def _apply_update_icon_area_labels(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels to the entity registry after a helper update."""
    if icon is None and area_id is None and labels is None:
        return
    registry_update: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if icon is not None:
        registry_update["icon"] = icon if icon else None
    if area_id is not None:
        registry_update["area_id"] = area_id if area_id else None
    if labels is not None:
        registry_update["labels"] = labels
    async with registry_update_lock("entity", entity_id):
        reg_result = await client.send_websocket_message(registry_update)
    if reg_result.get("success"):
        if icon is not None:
            updated_data["icon"] = icon if icon else None
        if area_id is not None:
            updated_data["area_id"] = area_id if area_id else None
        if labels is not None:
            updated_data["labels"] = labels
    else:
        warnings.append(
            f"Config updated but entity registry update failed: {_ws_error_msg(reg_result)}"
        )


async def _apply_update_registry_and_category(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels/category to the entity registry after a helper update."""
    await _apply_update_icon_area_labels(
        client, entity_id, icon, area_id, labels, updated_data, warnings
    )

    if category is not None:
        cat_result: dict[str, Any] = {}
        await apply_entity_category(
            client, entity_id, category, "helpers", cat_result, "helper"
        )
        if "category" in cat_result:
            updated_data["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(cat_result["warnings"])


@legacy_errors
async def _execute_update_simple_helper(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
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
    async with tools._schema_context(helper_type, "update"):
        return await tools._update_simple(
            helper_type,
            helper_id or "",
            entity_id,
            name,
            icon,
            area_id,
            labels,
            category,
            wait,
            MandatoryBPS,
            fields,
        )
