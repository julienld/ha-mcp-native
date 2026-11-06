"""A tag's name and icon in its snapshot and on restore.

Core keeps a tag's name in the entity registry, not in the tag store:
``tag/list`` fills ``name`` with ``entity.name or entity.original_name`` (the
default "Tag <id>"), and ``tag/update`` writes any name it is sent into the
registry. So the snapshot records the registry's own name (``None`` when the
tag was never named), and a restore sends a name only when there was one.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import registry_row


async def _tag_entity(client: Any, tag_id: str) -> dict[str, Any] | None:
    return await registry_row(client, "tag", tag_id)


async def tag_snapshot(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    """The listed tag with its registry name instead of the listed fallback."""
    entity = await _tag_entity(client, str(item.get("id")))
    return {
        **item,
        "name": entity.get("name") if entity else item.get("name"),
        "registry_icon": entity.get("icon") if entity else None,
    }


async def restore_tag(client: Any, tag_id: str, payload: dict[str, Any]) -> Any:
    from .backends.remote_snapshot_storage import RemoteStoredRestorePorts
    from .runtime.snapshot_storage_restore import restore_tag as restore

    return await restore(
        RemoteStoredRestorePorts(client, "tag"),
        tag_id,
        {key: value for key, value in payload.items() if key not in ("type", "tag_id")},
    )
