"""The entity-removal public contract, independent of its execution owner."""

from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class EntityRemovals(Protocol):
    async def remove(self, entity_id: str | list[str]) -> dict[str, Any]: ...


class EntityRemovalTools:
    def __init__(self, removals: EntityRemovals) -> None:
        self.removals = removals

    async def ha_remove_entity(
        self,
        entity_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Entity ID, or a list of entity IDs, to remove from the "
                    "entity registry (e.g., 'sensor.old_temperature')."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Remove one or more entities from the Home Assistant entity registry.

        Permanently removes the entity registration; the entity will no longer
        appear in the UI or be available to automations. Use only for orphaned
        or stale entity entries: if the underlying device, integration or
        stored helper is still there, the entity may be re-added on the next HA
        restart or reload; for a stored helper's entity a single-id removal
        names the tool that removes the helper. This cannot be undone without restoring
        from backup. For most use cases, consider disabling instead:
        ha_set_entity(entity_id="sensor.old", enabled=False).

        BULK MODE:
        Pass a list of entity IDs to remove up to 100 at once — handy for
        clearing the restored=true orphans an integration leaves behind after
        its filters change. Removals run sequentially and return:
          {removed: [...], skipped: [...], errors: [{entity_id, code, message}]}
        where skipped = ids already absent (not-found is idempotent, not an
        error). Each existing target is auto-backed-up under its durable registry ID.

        EXAMPLES:
        - Remove orphaned sensor: ha_remove_entity("sensor.old_temperature")
        - Bulk cleanup: ha_remove_entity(["sensor.orphan_1", "sensor.orphan_2"])

        Use ha_search / ha_get_entity to verify an entity before removing it.
        """
        return await self.removals.remove(entity_id)
