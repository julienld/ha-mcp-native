"""Direct storage-collection reads and optimistic helper update admission."""

import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

from ..helper_collections import SIMPLE_HELPER_TYPES, _apply_registry, collection_owner


def list_storage_items(hass: HomeAssistant, helper_type: str) -> dict[str, Any]:
    if helper_type not in SIMPLE_HELPER_TYPES:
        raise UnsupportedOperation(f"helpers.storage.{helper_type}")
    owner = collection_owner(hass, helper_type)
    if owner is None:
        raise UnsupportedOperation(f"helpers.storage.{helper_type}")
    collection = owner.storage_collection
    items = [dict(item) for item in collection.data.values()]
    if helper_type == "tag":
        # Core TagDictStorageCollectionWebsocket.ws_list_item projects names
        # from the registry; storage intentionally omits them after restart.
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        for item in items:
            item.pop("migrated", None)
            entity_id = registry.async_get_entity_id("tag", "tag", item["id"])
            if entity_id and (row := registry.async_get(entity_id)) is not None:
                item["name"] = row.name or row.original_name
    # Core's person/list includes YAML persons. The actual collection exposes
    # its YAML sibling, so collision checks keep the same complete input.
    if helper_type == "person":
        items.extend(dict(item) for item in collection.yaml_collection.async_items())
    return {"items": items}


def check_expected_item(
    hass: HomeAssistant, arguments: dict[str, Any], expected: Any
) -> None:
    if expected is None:
        return
    owner = collection_owner(hass, arguments["helper_type"])
    current = (
        owner.storage_collection.data.get(arguments.get("item_id")) if owner else None
    )
    if not json_values_equal(json.loads(json_bytes(current)), expected):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "The helper changed after its snapshot was read; no update was sent.",
            status="not_started",
        )


async def _list_storage(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return list_storage_items(hass, arguments["helper_type"])


async def _flow_entries(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    return {
        "items": [
            {"id": entry.entry_id, "name": entry.title}
            for entry in hass.config_entries.async_entries(arguments["helper_type"])
        ]
    }


async def _update_entities(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.helpers import entity_registry

    registry = entity_registry.async_get(hass)
    warnings: list[str] = []
    applied = [
        {
            "entity_id": entity_id,
            **_apply_registry(registry, entity_id, arguments["registry"], warnings),
        }
        for entity_id in arguments["entity_ids"]
    ]
    return {"applied": applied, "warnings": warnings}


DIRECT_OPERATIONS = {
    "helpers.storage.list": (_list_storage, False),
    "helpers.flow.entries": (_flow_entries, False),
    "helpers.entities.update": (_update_entities, True),
}
