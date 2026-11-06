"""Core owners supply data and mutations to the shared flow-restore algorithm."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.exceptions import HomeAssistantError as CoreError

from ha_mcp.errors import HomeAssistantError
from ha_mcp.runtime.flows.session import FlowSession
from ha_mcp.runtime.snapshot_entity_restore import _restore_entity_ids
from ha_mcp.runtime.snapshot_flow_restore import _flow_recreation_preflight
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    _FlowHelperReadError,
    _subentry_target,
)
from ha_mcp.tool_api import CallContext, ToolFailure

from .native_operations.snapshot_sources import (
    flow_snapshot,
    optional_subentry_snapshot,
    subentry_snapshots,
)

_LOGGER = logging.getLogger(__name__)


class NativeSnapshotFlowPorts:
    errors: tuple[type[BaseException], ...] = (
        HomeAssistantError,
        CoreError,
        ToolFailure,
        OSError,
        TimeoutError,
        PermissionError,
    )

    def __init__(
        self,
        manager: Any,
        actor: CallContext,
        expected_modified_at: float | None = None,
    ) -> None:
        self.manager, self.actor = manager, actor
        self.session = FlowSession(
            manager.backend, actor, expected_modified_at=expected_modified_at
        )

    async def helper(self, entry_id: str, helper_type: str) -> Any:
        await self.manager.backend.authorize(self.actor)
        try:
            return await flow_snapshot(
                self.manager.hass, {"entry_id": entry_id, "helper_type": helper_type}
            )
        except _FlowHelperReadError as error:
            if error.reason == "entry_missing":
                return None
            raise

    async def subentry(self, target: str) -> Any:
        await self.manager.backend.authorize(self.actor)
        entry_id, subentry_id = _subentry_target(target)
        return await optional_subentry_snapshot(
            self.manager.hass, {"entry_id": entry_id, "subentry_id": subentry_id}
        )

    async def subentries(self, entry_id: str) -> list[dict[str, Any]]:
        await self.manager.backend.authorize(self.actor)
        return await subentry_snapshots(self.manager.hass, entry_id)

    async def entries(self) -> list[dict[str, Any]]:
        await self.manager.backend.authorize(self.actor)
        return [
            {"entry_id": entry.entry_id}
            for entry in self.manager.hass.config_entries.async_entries()
        ]

    async def preflight(
        self, entry_id: str, snapshot: dict[str, Any], helper_type: str
    ) -> list[dict[str, Any]] | None:
        return await _flow_recreation_preflight(self, entry_id, snapshot, helper_type)

    def _registry(self) -> Any:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(self.manager.hass)

    @staticmethod
    def _row(entry: Any) -> dict[str, Any]:
        return {
            key: getattr(entry, key, None)
            for key in (
                "entity_id",
                "unique_id",
                "config_entry_id",
                "name",
                "original_name",
                "id",
            )
        }

    async def created(self, entry_id: str, unique_id: str) -> dict[str, Any]:
        async with asyncio.timeout(5):
            while True:
                await self.manager.backend.authorize(self.actor)
                rows = [
                    row
                    for row in self._registry().entities.values()
                    if row.config_entry_id == entry_id and row.unique_id == unique_id
                ]
                if len(rows) > 1:
                    raise HomeAssistantError("Recreated entity mapping is ambiguous")
                if rows:
                    return self._row(rows[0])
                await asyncio.sleep(0.1)

    def _check_collision(self, target: str, owned_entry_id: str | None = None) -> None:
        registry = self._registry()
        entry = registry.async_get(target)
        owned = (
            entry is not None
            and owned_entry_id is not None
            and entry.config_entry_id == owned_entry_id
        )
        if (
            entry is not None or self.manager.hass.states.get(target) is not None
        ) and not owned:
            raise BackupRestoreError(
                "The saved entity ID is occupied; it will not be overwritten",
                reason="entity_id_collision",
                conflicting_entity_id=target,
            )

    async def collision(
        self, target: str, *, owned_entry_id: str | None = None
    ) -> None:
        await self.manager.backend.authorize(self.actor)
        self._check_collision(target, owned_entry_id)

    async def check_collision(self, entity_id: str) -> None:
        await self.collision(entity_id)

    async def restore_ids(
        self, entry_id: str, old_entry: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        return await _restore_entity_ids(self, entry_id, old_entry, rows)

    async def rename(
        self,
        entry_id: str,
        unique_id: str,
        changes: dict[str, Any],
        expected: dict[str, Any],
    ) -> None:
        await self.manager.backend.authorize(self.actor)
        self.manager.snapshots._require_restore_access(self.actor)
        registry = self._registry()
        source = changes["entity_id"]
        current = registry.async_get(source)
        if (
            current is None
            or current.config_entry_id != entry_id
            or current.unique_id != unique_id
            or self._row(current) != expected
        ):
            raise BackupRestoreError(
                "Recreated entity identity changed before its registry update",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        target = changes.get("new_entity_id", source)
        if target != source:
            self._check_collision(target)
        # Core owns the atomic registry update and its notifications. No await
        # separates this final identity/permission check from that callback.
        try:
            registry.async_update_entity(
                source,
                **{
                    key: changes[key]
                    for key in ("new_entity_id", "name")
                    if key in changes
                },
            )
        except Exception as error:
            raise BackupRestoreError(
                "Recreated entity registry update did not complete normally",
                apply_status="unknown",
                reason="entity_rename_outcome_unknown",
                verification_status="unavailable",
            ) from error

    def log_failure(self, step: str, error: BaseException) -> None:
        _LOGGER.warning(
            "Native snapshot restore step=%s failed (%s)", step, type(error).__name__
        )
