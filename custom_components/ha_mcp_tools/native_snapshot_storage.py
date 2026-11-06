"""Core collection and registry ports for shared zone/tag restoration."""

from __future__ import annotations

import copy
from typing import Any

from ha_mcp.runtime.snapshot_payloads import BackupRestoreError
from ha_mcp.runtime.snapshot_storage_restore import StoredZoneMissing
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .native_snapshot_domains import StorageBefore


class NativeStoredRestorePorts:
    def __init__(
        self, manager: Any, actor: CallContext, domain: str, before: StorageBefore
    ) -> None:
        self.manager, self.actor, self.domain = manager, actor, domain
        self.before = before
        self.expected = copy.deepcopy(before.registry)

    async def update(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if self.before.item is None:
            if self.domain == "zone":
                raise StoredZoneMissing
            raise ToolFailure(
                "RESOURCE_NOT_FOUND", "Tag no longer exists", status="not_started"
            )
        result = await self._write("update", fields, item_id)
        if self.domain == "tag" and "name" in fields:
            self.expected["values"]["name"] = fields["name"]
        return result

    async def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        result = await self._write("create", fields)
        entity = await self.entity(str(result["id"]))
        if entity is not None:
            self.expected = self._expected(entity)
        return result

    async def _write(
        self, action: str, fields: dict[str, Any], item_id: str | None = None
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "helper_type": self.domain,
            "action": action,
            "data": fields,
            "registry": {},
        }
        if action == "update":
            arguments.update(
                item_id=item_id,
                expected_item=self.before.item,
                expected_registry=self.before.registry,
            )
        result = await self.manager.backend.execute(
            "helpers.storage.write", arguments, self.actor
        )
        if not result.get("success"):
            raise ToolFailure(
                "RESTORE_FAILED",
                "Core rejected the stored item restore",
                status="not_started",
            )
        item: dict[str, Any] = result["item"]
        return (
            {**item, "warnings": result["warnings"]} if result.get("warnings") else item
        )

    async def items(self) -> list[dict[str, Any]]:
        result = await self.manager.backend.execute(
            "helpers.storage.list", {"helper_type": self.domain}, self.actor
        )
        items: list[dict[str, Any]] = result["items"]
        return items

    async def entity(self, item_id: str) -> dict[str, Any] | None:
        await self.manager.backend.authorize(self.actor)
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.manager.hass)
        entity_id = registry.async_get_entity_id(self.domain, self.domain, item_id)
        row = registry.async_get(entity_id) if entity_id else None
        if row is None:
            return None
        return {
            key: getattr(row, key)
            for key in ("entity_id", "id", "platform", "unique_id", "name", "icon")
        }

    @staticmethod
    def _expected(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: row[key] for key in ("entity_id", "id", "platform", "unique_id")},
            "values": {"name": row["name"], "icon": row["icon"]},
        }

    async def clear_name(self, entity: dict[str, Any]) -> None:
        await self._change_registry(entity["entity_id"], {"name": None})

    async def _change_registry(self, entity_id: str, changes: dict[str, Any]) -> None:
        from homeassistant.helpers import entity_registry as er

        try:
            await self.manager.backend.authorize(self.actor)
            self.manager.snapshots._require_restore_access(self.actor)
            registry = er.async_get(self.manager.hass)
            row = registry.async_get(entity_id)
            expected = self.expected
            if (
                row is None
                or any(
                    getattr(row, key) != expected[key]
                    for key in ("entity_id", "id", "platform", "unique_id")
                )
                or any(
                    not json_values_equal(getattr(row, key), value)
                    for key, value in expected["values"].items()
                )
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "The entity registry changed during restore",
                    status="not_started",
                )
            # No suspension between the last admission/identity check and Core's write.
            registry.async_update_entity(entity_id, **changes)
            self.expected["values"].update(changes)
        except (PermissionError, ToolFailure, ValueError) as error:
            raise BackupRestoreError(
                "The stored item was restored, but its entity registry was not updated",
                apply_status="applied",
                reason="registry_update_failed",
            ) from error

    async def icon(self, item_id: str, value: str | None) -> list[str]:
        try:
            entity = await self.entity(item_id)
            if entity is None:
                return (
                    []
                    if value is None
                    else [
                        f"The registry icon {value} was not restored: {self.domain} {item_id} has no entity."
                    ]
                )
            async with registry_update_lock("entity", entity["entity_id"]):
                await self._change_registry(entity["entity_id"], {"icon": value})
        except (BackupRestoreError, PermissionError, ToolFailure):
            return [
                f"The {self.domain} was restored, but its registry icon was not: the registry changed or administrator admission was revoked."
            ]
        return []
