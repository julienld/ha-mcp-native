"""Helper catalog and field-description reads from the existing Core owners."""

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp.runtime.helper_listing_shape import (
    _component_covers,
    _raise_flow_requires_component,
    _shape_component_helpers_response,
    raise_if_helper_flows_degraded,
    shape_all_helpers_response,
)
from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.tool_api import UnsupportedOperation

from ..helper_collections import read_item
from ..websocket_api.overview import _do_helpers_list, _helpers_list_prep
from .helpers import list_storage_items


async def _tag_listing(hass: HomeAssistant, helper_type: str) -> dict[str, Any]:
    # Core's collection helpers have a state; tags are the sole uncovered
    # storage type in helpers_list. They carry no entity-registry metadata.
    if helper_type != "tag":
        raise UnsupportedOperation(f"helpers.catalog.{helper_type}")
    items = list_storage_items(hass, "tag")["items"]
    return {
        "success": True,
        "helper_type": "tag",
        "count": len(items),
        "helpers": items,
        "message": f"Found {len(items)} tag helper(s)",
    }


async def catalog(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    helper_type = arguments["helper_type"]
    is_flow = helper_type not in SIMPLE_HELPER_TYPES
    params = {"include_flow_helpers": is_flow}
    if helper_type != "all":
        params["helper_types"] = [helper_type]
    result = _do_helpers_list(hass, params, **await _helpers_list_prep(hass, params))
    if helper_type == "all":
        from homeassistant.loader import async_get_config_flows

        return await shape_all_helpers_response(
            result,
            lambda kind: _tag_listing(hass, kind),
            flow_types=frozenset(
                await async_get_config_flows(hass, type_filter="helper")
            ),
        )
    if not _component_covers(result, helper_type):
        if is_flow:
            raise_if_helper_flows_degraded(result, [helper_type])
            _raise_flow_requires_component(helper_type)
        return await _tag_listing(hass, helper_type)
    return _shape_component_helpers_response(helper_type, result)


async def description_item(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    helper_id = arguments["helper_id"]
    result = read_item(
        hass,
        er.async_get(hass),
        arguments["helper_type"],
        helper_id if "." in helper_id else None,
        None if "." in helper_id else helper_id,
    )
    item = result.get("item") if result.get("success") is True else None
    return item if isinstance(item, dict) else None


async def translations(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.helpers.translation import async_get_translations

    if arguments["category"] not in {"config", "options"}:
        raise UnsupportedOperation("helpers.translations.category")
    return {
        "resources": await async_get_translations(
            hass, "en", arguments["category"], {arguments["handler"]}
        )
    }


DIRECT_OPERATIONS: dict[
    str, tuple[Callable[[HomeAssistant, dict[str, Any]], Awaitable[Any]], bool]
] = {
    "helpers.catalog": (catalog, False),
    "helpers.storage.describe_item": (description_item, False),
    "helpers.translations": (translations, False),
}
