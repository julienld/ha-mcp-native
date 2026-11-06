"""Public backup routing uses Core archives or the existing edit snapshot store."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.backup_controls import gate_backup_combo, require_backup_param
from ha_mcp.runtime.snapshot_files import SnapshotInUseError
from ha_mcp.tool_api import CallContext, ToolFailure

from .edit_backups import NativeEditBackups
from .full_backup_restore import schedule_full_restore
from .full_backups import NativeFullBackups
from .registry_access import registry_failure


class NativeBackups:
    def __init__(self, manager: Any, actor: CallContext) -> None:
        self.manager, self.actor = manager, actor
        self.edits = NativeEditBackups(manager, actor)

    async def manage(
        self,
        *,
        scope: str,
        action: str,
        name: str | None,
        backup_id: str | None,
        restore_database: bool | None,
        restore_addons: list[str] | None,
        restore_folders: list[str] | None,
        safety_backup: bool,
        parts: dict[str, Any],
        confirm: bool,
        domain: str | None,
        entity_id: str | None,
        backup_name: str | None,
        older_than_days: int | None,
        limit: int,
        ctx: Any = None,
    ) -> dict[str, Any]:
        try:
            gate_backup_combo(scope, action)
            self._check_arguments(
                scope,
                action,
                {
                    "name": name,
                    "backup_id": backup_id,
                    "restore_database": restore_database,
                    "restore_addons": restore_addons,
                    "restore_folders": restore_folders,
                    "safety_backup": safety_backup or None,
                    "confirm": confirm or None,
                    "domain": domain,
                    "entity_id": entity_id,
                    "backup_name": backup_name,
                    "older_than_days": older_than_days,
                    "limit": limit if limit != 200 else None,
                    **parts,
                },
            )
            if scope == "edits":
                return await self.edits.manage(
                    action,
                    domain=domain,
                    entity_id=entity_id,
                    backup_name=backup_name,
                    older_than_days=older_than_days,
                    limit=limit,
                )
            full = NativeFullBackups(self.manager, self.actor)
            if action == "create":
                return await full.create(name, parts=parts)
            if action == "list":
                return await full.list(limit)
            identifier = require_backup_param("backup_id", backup_id, scope, action)
            if action == "delete":
                return await full.delete(identifier, confirm)
            return await schedule_full_restore(
                self.manager,
                self.actor,
                identifier,
                restore_database,
                restore_addons=restore_addons,
                restore_folders=restore_folders,
                safety_backup=safety_backup,
            )
        except Exception as error:
            failure = self._failure(error, backup_name)
            if self.edits.completed and failure.status in {None, "not_started"}:
                failure.status = "confirmed"
                failure.data.update(write_completed=True, requires_reread=True)
            raise failure from error

    @staticmethod
    def _check_arguments(scope: str, action: str, values: dict[str, Any]) -> None:
        uses = {
            "name": {("snapshot", "create")},
            "backup_id": {("snapshot", "restore"), ("snapshot", "delete")},
            **{
                key: {("snapshot", "restore")}
                for key in (
                    "restore_database",
                    "restore_addons",
                    "restore_folders",
                    "safety_backup",
                )
            },
            **{
                key: {("snapshot", "create")}
                for key in (
                    "include_homeassistant",
                    "include_database",
                    "include_addons",
                    "include_all_addons",
                    "include_folders",
                )
            },
            "confirm": {("snapshot", "delete")},
            "domain": {("edits", mode) for mode in ("create", "list", "delete")},
            "entity_id": {("edits", mode) for mode in ("create", "list", "delete")},
            "backup_name": {
                ("edits", mode) for mode in ("view", "diff", "restore", "delete")
            },
            "older_than_days": {("edits", "delete")},
            "limit": {("edits", "list"), ("snapshot", "list")},
        }
        misplaced = [
            key
            for key, value in values.items()
            if value is not None and (scope, action) not in uses.get(key, set())
        ]
        if misplaced:
            raise ValueError(
                f"Parameters do not apply to {scope}.{action}: {', '.join(misplaced)}"
            )
        if (scope, action) == ("edits", "delete") and values["backup_name"] is not None:
            filters = [
                key
                for key in ("domain", "entity_id", "older_than_days")
                if values[key] is not None
            ]
            if filters:
                raise ValueError(
                    "backup_name cannot be combined with deletion filters: "
                    + ", ".join(filters)
                )

    @staticmethod
    def _failure(error: Exception, name: str | None) -> ToolFailure:
        if isinstance(error, ToolFailure):
            return registry_failure(error, {"backup_name": name})
        if isinstance(error, SnapshotInUseError):
            payload = create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(error),
                context={"backup_name": name, "data": {"reason": "snapshot_in_use"}},
                suggestions=[
                    "Retry deletion after the active capture or restore finishes"
                ],
            )
        elif isinstance(error, FileNotFoundError):
            payload = create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Backup {name!r} not found",
                context={"backup_name": name},
            )
        elif isinstance(error, (ValueError, LookupError)):
            payload = create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(error),
                context={"backup_name": name},
            )
        else:
            return registry_failure(error, {"backup_name": name})
        return ToolFailure(
            payload["error"]["code"],
            payload["error"]["message"],
            data=payload,
            status="not_started",
        )
