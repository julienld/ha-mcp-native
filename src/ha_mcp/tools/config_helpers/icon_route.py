"""Route a storage helper's icon by the running Core collection's own fields."""

from typing import Any

from ha_mcp.runtime.helper_payload import collection_icon_from_schema

from .. import component_helper_collections


async def collection_icon(
    client: Any, helper_type: str, action: str, icon: str | None
) -> str | None:
    """The icon for the stored item, or ``None`` when it goes to the registry.

    The item takes the icon only when Core's ``action`` schema for the type
    declares an ``icon`` field (person and tag declare none). When the schemas
    cannot be read (no component, or a failed read) or do not list the type or
    action, nothing is guessed: the icon is entity-registry metadata, as the
    entity settings set it.

    A cleared icon ('') is returned as is, so the stored item drops its icon
    too; clearing only the registry would bring an older stored icon back. A
    zone's icon is returned as is for the #2643 rule in ``merged_update``.
    """
    if icon is None or icon == "" or helper_type == "zone":
        return icon
    schemas = await component_helper_collections.fetch_helper_schemas(client)
    return collection_icon_from_schema(helper_type, action, icon, schemas)
