"""Restore recreated entity identities through finite registry operations."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any, Protocol

from ha_mcp.utils.registry_update_lock import registry_update_lock

from .snapshot_payloads import BackupRestoreError


class EntityRestorePorts(Protocol):
    errors: tuple[type[BaseException], ...]

    async def created(self, entry_id: str, unique_id: str) -> dict[str, Any]: ...
    async def collision(
        self, target: str, *, owned_entry_id: str | None = None
    ) -> None: ...
    async def rename(
        self,
        entry_id: str,
        unique_id: str,
        changes: dict[str, Any],
        expected: dict[str, Any],
    ) -> Any: ...
    def log_failure(self, step: str, error: BaseException) -> None: ...


def _recreated_unique_id(saved: dict[str, Any], old_entry: str, new_entry: str) -> str:
    """Core derives a helper entity's unique_id from its config-entry ID."""
    return str(saved["unique_id"]).replace(old_entry, new_entry)


async def _restore_entity_ids(
    ports: EntityRestorePorts,
    entry_id: str,
    original_entry_id: str,
    saved_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Give each recreated entity its saved ID/name, guarded by ownership.

    A failure part-way reports the entities already restored, so the outcome
    names every rename that did happen.
    """
    mapping: list[dict[str, str]] = []
    try:
        for saved in saved_rows:
            await _restore_entity_id(ports, entry_id, original_entry_id, saved, mapping)
    except BackupRestoreError as err:
        err.outcome.setdefault("entity_id_mapping", mapping)
        raise
    except ports.errors as err:
        ports.log_failure("entity_rename", err)
        raise BackupRestoreError(
            "A recreated entity could not be found or restored",
            reason="upstream_error",
            entity_id_mapping=mapping,
            verification_status="unavailable",
        ) from err
    return mapping


async def _restore_entity_id(
    ports: EntityRestorePorts,
    entry_id: str,
    original_entry_id: str,
    saved: dict[str, Any],
    mapping: list[dict[str, str]],
) -> None:
    """Rename one recreated entity to its saved ID/name and record it."""
    unique_id = _recreated_unique_id(saved, original_entry_id, entry_id)
    row = await ports.created(entry_id, unique_id)
    source, target = (row["entity_id"], saved["entity_id"])
    locked_ids = {source, target}
    async with AsyncExitStack() as locks:
        for entity_id in sorted(locked_ids):
            await locks.enter_async_context(registry_update_lock("entity", entity_id))
        row = await ports.created(entry_id, unique_id)
        source = row["entity_id"]
        if source not in locked_ids:
            raise BackupRestoreError(
                "Recreated entity ID changed while waiting to restore it",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        if source.split(".")[0] != target.split(".")[0]:
            raise BackupRestoreError(
                "Recreated entity has an unexpected domain",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        await ports.collision(target, owned_entry_id=entry_id)
        update: dict[str, Any] = {"entity_id": source}
        if source != target:
            update["new_entity_id"] = target
        if row.get("name") != saved.get("name"):
            update["name"] = saved.get("name")
        if len(update) > 1:
            try:
                await ports.rename(entry_id, unique_id, update, row)
            except BackupRestoreError:
                raise
            except ports.errors as err:
                ports.log_failure("entity_rename", err)
                raise BackupRestoreError(
                    "The recreated helper's entity rename outcome is unknown",
                    reason="entity_rename_outcome_unknown",
                    entity_id_mapping=[
                        *mapping,
                        {"created_entity_id": source, "target_entity_id": target},
                    ],
                    verification_status="unavailable",
                ) from err
        actual = await ports.created(entry_id, unique_id)
        if actual["entity_id"] != target or actual.get("name") != saved.get("name"):
            raise BackupRestoreError(
                "Recreated entity identity did not match",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        mapping.append({"created_entity_id": source, "restored_entity_id": target})
