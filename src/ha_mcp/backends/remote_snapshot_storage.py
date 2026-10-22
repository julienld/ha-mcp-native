"""Remote storage/registry operations for the shared snapshot restore rules."""

from typing import Any, cast

from ha_mcp.errors import HomeAssistantCommandError
from ha_mcp.runtime.snapshot_storage_restore import StoredZoneMissing


def _manager() -> Any:
    from ha_mcp import backup_manager

    return backup_manager


class RemoteStoredRestorePorts:
    def __init__(self, client: Any, domain: str) -> None:
        self.client, self.domain = client, domain

    async def update(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                await _manager()._ws_send(
                    self.client,
                    {
                        "type": f"{self.domain}/update",
                        f"{self.domain}_id": item_id,
                        **fields,
                    },
                ),
            )
        except HomeAssistantCommandError as error:
            if self.domain == "zone" and error.code == "not_found":
                raise StoredZoneMissing from error
            raise

    async def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await _manager()._ws_send(
                self.client, {"type": f"{self.domain}/create", **fields}
            ),
        )

    async def items(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            _manager()._require_list(
                await _manager()._ws_send(self.client, {"type": f"{self.domain}/list"}),
                f"{self.domain}/list",
            ),
        )

    async def entity(self, item_id: str) -> dict[str, Any] | None:
        from ha_mcp.backup_entity_ids import registry_row

        return await registry_row(self.client, self.domain, item_id)

    async def clear_name(self, entity: dict[str, Any]) -> None:
        await _manager()._ws_send(
            self.client,
            {
                "type": "config/entity_registry/update",
                "entity_id": entity["entity_id"],
                "name": None,
            },
        )

    async def icon(self, item_id: str, value: str | None) -> list[str]:
        from ha_mcp.backup_entity_ids import restore_registry_icon

        return await restore_registry_icon(self.client, self.domain, item_id, value)
