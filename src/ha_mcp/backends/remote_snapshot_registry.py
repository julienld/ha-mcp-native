"""Legacy snapshot handler projection over the shared registry recovery body."""

from typing import Any

from ha_mcp.runtime.snapshot_registry_restore import current_registry, restore_registry
from ha_mcp.tool_api import CallContext

from .remote import RemoteBackend


async def restore_registry_snapshot(
    client: Any, domain: str, entity_id: str, config: Any
) -> Any:
    backend, actor = RemoteBackend(client), CallContext(None)
    before = await current_registry(backend, actor, domain, entity_id, config)
    return await restore_registry(backend, actor, domain, entity_id, config, before)
