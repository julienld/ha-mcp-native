"""Legacy snapshot handler projection over the shared registry recovery body."""

from typing import Any

from ha_mcp.runtime.snapshot_registry_restore import (
    current_registry,
    registry_target,
    restore_registry,
)
from ha_mcp.tool_api import CallContext
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .remote import RemoteBackend


async def restore_registry_snapshot(
    client: Any, domain: str, entity_id: str, config: Any
) -> Any:
    backend, actor = RemoteBackend(client), CallContext(None)
    kind, target, _ = registry_target(domain, entity_id, config)
    identity = (
        f"{target['scope']}:{target['category_id']}"
        if kind == "category"
        else target[f"{kind}_id"]
    )
    async with registry_update_lock(kind, identity):
        before = await current_registry(backend, actor, domain, entity_id, config)
        return await restore_registry(backend, actor, domain, entity_id, config, before)
