"""Explicit edit backups reuse the common store and native restore coordinator."""

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ha_mcp.runtime.backup_controls import (
    _enforce_backup_controls,
    require_backup_param,
)
from ha_mcp.runtime.snapshot_compare import diff_snapshot_values
from ha_mcp.runtime.snapshot_files import SCHEMA_VERSION, SnapshotInUseError
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    MandatoryBackupError,
    _FlowHelperReadError,
    validate_snapshot_identity,
)
from ha_mcp.runtime.snapshot_projection import restore_error
from ha_mcp.runtime.snapshot_restore import restore_snapshot_data
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..legacy_backup_files import (
    _LEGACY_BACKUP_DIRNAME,
    _list_legacy_backups_sync,
    read_legacy_backup,
)
from ..native_backup_visibility import guard_snapshot_visibility
from ..native_snapshot_restore import NativeRestoreOperations, restore_controls
from .backup_capture import NativeCaptureSources
from .registry_access import admit_registry_write, authorize_actor

LEGACY_PREFIX = "legacy:"


class NativeEditBackups:
    def __init__(self, manager: Any, actor: CallContext) -> None:
        self.manager, self.actor = manager, actor
        self.snapshots = manager.snapshots
        self.completed = False

    async def guard(self, action: str) -> None:
        writes = action in {"create", "restore", "delete"}
        if writes:
            await admit_registry_write(self.actor, self.manager.backend.authorize)
        else:
            await authorize_actor(self.actor, self.manager.backend.authorize)
        _enforce_backup_controls(
            SimpleNamespace(
                enable_snapshot_actions=self.manager.policy.flag(
                    "enable_snapshot_actions"
                ),
                backup_read_only=self.manager.policy.flag("backup_read_only"),
            ),
            "edits",
            action,
            not writes,
        )

    async def _read_legacy(self, name: str) -> dict[str, Any]:
        data: dict[str, Any] = await self.snapshots._io(
            read_legacy_backup,
            Path(self.manager.hass.config.path()),
            name[len(LEGACY_PREFIX) :],
        )
        if not data.get("success"):
            if "does not exist" in str(data.get("error", "")):
                raise FileNotFoundError(name)
            raise ValueError(str(data.get("error", "Legacy backup could not be read")))
        return data

    async def list(
        self, domain: str | None, entity_id: str | None, limit: int
    ) -> dict[str, Any]:
        await self.guard("list")
        legacy = []
        warnings = []
        if domain in {None, "yaml_file"} and entity_id is None:
            try:
                rows = await self.snapshots._io(
                    _list_legacy_backups_sync,
                    Path(self.manager.hass.config.path(_LEGACY_BACKUP_DIRNAME)),
                )
                legacy = [
                    {
                        "name": LEGACY_PREFIX + row["filename"],
                        "domain": "yaml_file",
                        "entity_id": row.get("file_path"),
                        "timestamp": row.get("timestamp"),
                        "size": row.get("size"),
                        "source": "legacy",
                        "path_ambiguous": row.get("path_ambiguous", True),
                    }
                    for row in rows
                ]
            except OSError as error:
                warnings.append(
                    f"Legacy .bak backups could not be listed and are omitted: {type(error).__name__}"
                )
        store = self.snapshots._store()
        rows = await self.snapshots._io(
            partial(
                store.list_snapshots,
                domain=domain,
                entity_id=entity_id,
                limit=max(1, limit - len(legacy)),
            )
        )
        rows = (rows + legacy)[:limit]
        await self.guard("list")
        return {
            "success": True,
            "data": {
                "backups": rows,
                "count": len(rows),
                "backup_dir": str(store.directory),
                "enabled": self.manager.policy.flag("enable_auto_backup"),
                "throttle_minutes": store.throttle_seconds / 60,
                "retain_per_entity": store.retention,
            },
            **({"warnings": warnings} if warnings else {}),
        }

    async def view(self, name: str) -> dict[str, Any]:
        await self.guard("view")
        if name.startswith(LEGACY_PREFIX):
            data = await self._read_legacy(name)
        else:
            data = await self.snapshots._io(self.snapshots._store().read_snapshot, name)
        await guard_snapshot_visibility(self.manager, self.actor, "view", data)
        await self.guard("view")
        return {"success": True, "data": data}

    async def create(self, domain: str, identifier: str) -> dict[str, Any]:
        await self.guard("create")
        try:
            identifier, current = await NativeCaptureSources(
                self.manager, self.actor
            ).read(domain, identifier)
        except _FlowHelperReadError as error:
            raise ToolFailure(
                "BACKUP_CAPTURE_FAILED",
                f"The helper's complete restorable configuration could not be captured ({error.reason})",
                status="not_started",
            ) from None
        if current is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "The selected backup target has no restorable configuration",
                status="not_started",
            )
        data = {"domain": domain, "entity_id": identifier, "config": current}
        await guard_snapshot_visibility(self.manager, self.actor, "create", data)
        store = self.snapshots._store()

        async def worker() -> Path:
            await self.guard("create")
            try:
                path: Path | None = await self.snapshots._io(
                    partial(
                        store.capture,
                        domain,
                        identifier,
                        current,
                        "ha_manage_backup.edits.create",
                        force=True,
                    )
                )
                if path is None:
                    raise RuntimeError("The edit snapshot was not written")
                self.completed = True
                return path
            except (OSError, RuntimeError) as error:
                raise ToolFailure(
                    "OUTCOME_UNKNOWN",
                    f"Edit snapshot persistence did not complete normally ({type(error).__name__}); inspect the backup list before retrying",
                    status="outcome_unknown",
                ) from None

        path = await await_owned_io(worker())
        size = await self.snapshots._io(lambda: path.stat().st_size)
        await self.guard("create")
        return {
            "success": True,
            "data": {
                "backup_name": path.name,
                "domain": domain,
                "entity_id": identifier,
                "size": size,
            },
        }

    async def _comparison(self, name: str) -> tuple[dict[str, Any], Any]:
        if not name.startswith(LEGACY_PREFIX):
            result: tuple[
                dict[str, Any], Any
            ] = await self.snapshots.snapshot_comparison(name, self.actor)
            return result
        info = await self._read_legacy(name)
        data = _legacy_snapshot(info)
        current = None
        if not info.get("path_ambiguous", True) and info.get("file_path"):
            _identifier, current = await NativeCaptureSources(
                self.manager, self.actor
            ).read("yaml_file", info["file_path"])
        return data, current

    async def diff(self, name: str) -> dict[str, Any]:
        await self.guard("diff")
        data, current = await self._comparison(name)
        await guard_snapshot_visibility(self.manager, self.actor, "diff", data, current)
        diff = await self.snapshots._io(diff_snapshot_values, name, data, current)
        await self.guard("diff")
        warnings = []
        if diff.get("entity_missing"):
            warnings.append(
                "Entity is missing from HA; restore behaviour is domain-dependent (upsert paths recreate it; update-only paths return an error)"
            )
        if diff.get("truncated"):
            noun = "Diff" if diff.get("kind") == "text" else "Patch"
            warnings.append(
                f"{noun} truncated; the target has more changes than the bounded diff captures — view the snapshot for the full state"
            )
        return {
            "success": True,
            "data": diff,
            **({"warnings": warnings} if warnings else {}),
        }

    async def restore(self, name: str) -> dict[str, Any]:
        await self.guard("restore")
        try:
            if name.startswith(LEGACY_PREFIX):
                result = await self._restore_legacy(name)
            else:
                result = await self.snapshots.restore_snapshot(name, self.actor)
        except BackupRestoreError as error:
            payload, _status = restore_error(error)
            raise ToolFailure(
                payload["error"]["code"],
                payload["error"]["message"],
                data=payload,
                status={
                    "not_applied": "not_started",
                    "applied": "confirmed",
                    "unknown": "outcome_unknown",
                }[error.outcome["apply_status"]],
            ) from error
        self.completed = True
        await self.guard("restore")
        data = dict(result)
        warnings = list(data.pop("warnings", []))
        if isinstance(result.get("result"), dict):
            nested = dict(result["result"])
            warnings.extend(nested.pop("warnings", []))
            data["result"] = nested
        if result.get("safety_backup"):
            warnings.append(
                "This restore did NOT restart HA. To revert, restore the safety_backup."
            )
        return {
            "success": True,
            "data": data,
            **({"warnings": list(dict.fromkeys(warnings))} if warnings else {}),
        }

    async def _restore_legacy(self, name: str) -> dict[str, Any]:
        info = await self._read_legacy(name)
        if info.get("path_ambiguous", True) or not info.get("file_path"):
            raise ValueError(
                "The legacy backup's original path is ambiguous. View its contents and restore them to the intended file manually."
            )
        data = _legacy_snapshot(info)
        validate_snapshot_identity(data)
        await guard_snapshot_visibility(self.manager, self.actor, "restore", data)
        store = self.snapshots._store()
        async with registry_update_lock(
            "snapshot_restore:yaml_file", data["entity_id"]
        ):
            with restore_controls(self.snapshots, self.actor):
                return await restore_snapshot_data(
                    store,
                    name,
                    data,
                    NativeRestoreOperations(self.snapshots, data, self.actor, store),
                    take_safety_backup=True,
                    restore_errors=(
                        OSError,
                        ValueError,
                        ToolFailure,
                        MandatoryBackupError,
                        PermissionError,
                    ),
                )

    async def delete(
        self,
        domain: str | None,
        entity_id: str | None,
        name: str | None,
        older_than_days: int | None,
    ) -> dict[str, Any]:
        await self.guard("delete")
        if (
            not name
            and domain is None
            and entity_id is None
            and older_than_days is None
        ):
            raise ValueError(
                "edits.delete requires backup_name OR at least one filter (domain, entity_id, older_than_days)"
            )
        store = self.snapshots._store()

        async def worker() -> dict[str, Any]:
            await self.guard("delete")
            try:
                if name:
                    await self.snapshots._io(store.delete_snapshot, name)
                    return {"success": True, "data": {"deleted": [name]}}
                bulk = await self.snapshots._io(
                    partial(
                        store.delete_bulk,
                        domain=domain,
                        entity_id=entity_id,
                        older_than_days=older_than_days,
                    )
                )
                warnings = (
                    [
                        f"Failed to delete {len(bulk['failed'])} backup(s); see server log"
                    ]
                    if bulk["failed"]
                    else []
                )
                count = sum(
                    reason == "snapshot_in_use"
                    for reason in bulk.get("failure_reasons", {}).values()
                )
                if count:
                    warnings.append(
                        f"{count} backup(s) are in use by a capture or restore; retry deletion after it finishes."
                    )
                return {
                    "success": True,
                    "data": {
                        **bulk,
                        "count": len(bulk["deleted"]),
                        "failed_count": len(bulk["failed"]),
                    },
                    **({"warnings": warnings} if warnings else {}),
                }
            except SnapshotInUseError:
                raise
            except OSError as error:
                if isinstance(error, FileNotFoundError):
                    raise
                raise ToolFailure(
                    "OUTCOME_UNKNOWN",
                    "Edit snapshot deletion did not complete normally; inspect the backup list before retrying",
                    status="outcome_unknown",
                ) from None

        result = await await_owned_io(worker())
        self.completed = True
        await self.guard("delete")
        return result

    async def manage(
        self,
        action: str,
        *,
        domain: str | None,
        entity_id: str | None,
        backup_name: str | None,
        older_than_days: int | None,
        limit: int,
    ) -> dict[str, Any]:
        if action == "list":
            return await self.list(domain, entity_id, limit)
        if action == "create":
            return await self.create(
                require_backup_param("domain", domain, "edits", action),
                require_backup_param("entity_id", entity_id, "edits", action),
            )
        if action == "delete":
            return await self.delete(domain, entity_id, backup_name, older_than_days)
        name = require_backup_param("backup_name", backup_name, "edits", action)
        return await {"view": self.view, "diff": self.diff, "restore": self.restore}[
            action
        ](name)


def _legacy_snapshot(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "domain": "yaml_file",
        "entity_id": info.get("file_path") or info["filename"],
        "config": info["content"],
        "kind": "text",
        "timestamp": info.get("timestamp"),
    }
