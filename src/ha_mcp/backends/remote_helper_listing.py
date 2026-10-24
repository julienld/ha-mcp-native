"""Remote helper description acquisition through the existing client."""

from typing import Any

from ha_mcp.tool_api import UnsupportedOperation


async def describe_operation(
    client: Any, operation: str, arguments: dict[str, Any]
) -> Any:
    if operation == "helpers.storage.describe_item":
        return await _stored_item(
            client, arguments["helper_type"], arguments["helper_id"]
        )
    if operation == "helpers.translations":
        return await client.send_websocket_message(
            {
                "type": "frontend/get_translations",
                "language": "en",
                "category": arguments["category"],
                "integration": [arguments["handler"]],
            }
        )
    if operation == "helpers.catalog":
        from ha_mcp.tools.tools_config_helpers import HelperConfigTools

        return await HelperConfigTools(client)._list_helper_records(
            arguments["helper_type"]
        )
    raise UnsupportedOperation(operation)


async def _stored_item(
    client: Any, helper_type: str, helper_id: str
) -> dict[str, Any] | None:
    from ha_mcp.runtime.helper_listing_shape import listed_items
    from ha_mcp.tools.component_helper_collections import read_helper_item

    by_entity = "." in helper_id
    read = await read_helper_item(
        client,
        helper_type,
        entity_id=helper_id if by_entity else None,
        item_id=None if by_entity else helper_id,
    )
    if read is not None:
        item = read.get("item", read)
        return item if isinstance(item, dict) else None
    item_id: Any = helper_id
    if by_entity:  # the legacy list is keyed by the entity's unique_id
        entry = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": helper_id}
        )
        registered = entry.get("result") if isinstance(entry, dict) else None
        item_id = registered.get("unique_id") if isinstance(registered, dict) else None
        if not item_id:
            return None
    result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    items = listed_items(result.get("result") if isinstance(result, dict) else result)
    return next(
        (i for i in items if isinstance(i, dict) and i.get("id") == item_id), None
    )
