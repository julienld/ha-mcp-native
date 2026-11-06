"""Pin a full-backup target through safety capture and deferred Core handoff."""

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io

from .full_backups import NativeFullBackups
from .registry_failures import with_recovery_hint


@dataclass(frozen=True, slots=True)
class FullRestoreTarget:
    owner: Any = field(repr=False)
    reader_writer: Any = field(repr=False)
    backup_id: str
    agent_id: str
    agent: Any = field(repr=False)
    password: str | None = field(repr=False)
    image: dict[str, Any] = field(repr=False)
    restore_database: bool
    restore_addons: tuple[str, ...]
    restore_folders: tuple[Any, ...]

    async def verify(self, manager: Any, actor: CallContext) -> None:
        access = NativeFullBackups(manager, actor)
        access.owner = self.owner
        await access.guard("restore")
        row, errors = await await_owned_io(self.owner.async_get_backup(self.backup_id))
        await manager.visibility.before(
            "ha_manage_backup",
            {"scope": "snapshot", "action": "restore", "backup_id": self.backup_id},
            actor,
        )
        await access.guard("restore")
        if errors or not json_values_equal(self.image, json.loads(json_bytes(row))):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The target backup changed or became unavailable during restore preparation",
                status="not_started",
            )
        agent_id, agent, password = access.credentials(
            self.agent_id, require_password=self.password is not None
        )
        if (
            agent_id != self.agent_id
            or agent is not self.agent
            or password != self.password
            or self.owner._reader_writer is not self.reader_writer
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Backup restore owner, protection or destination changed",
                status="not_started",
            )
        access.idle()

    def arguments(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "password": self.password,
            "restore_addons": list(self.restore_addons),
            "restore_folders": list(self.restore_folders),
            "restore_homeassistant": True,
            "restore_database": self.restore_database,
        }


async def schedule_full_restore(
    manager: Any,
    actor: CallContext,
    backup_id: str,
    restore_database: bool | None,
    *,
    restore_addons: list[str] | None = None,
    restore_folders: list[str] | None = None,
    safety_backup: bool = False,
) -> dict[str, Any]:
    access = NativeFullBackups(manager, actor)
    owner = await access.guard("restore")
    row, errors = await await_owned_io(owner.async_get_backup(backup_id))
    await access.guard("restore")
    if errors:
        raise ToolFailure(
            "SERVICE_CALL_FAILED",
            "The target backup could not be read completely",
            status="not_started",
        )
    if row is None:
        raise with_recovery_hint(
            ToolFailure(
                "RESOURCE_NOT_FOUND", "Target backup not found", status="not_started"
            ),
            "Use ha_manage_backup(scope='snapshot', action='list') to inspect available backups and select a current backup_id",
        )
    image = json.loads(json_bytes(row))
    effective, addons, folders = _restore_selection(
        access, image, restore_database, restore_addons, restore_folders
    )
    held_agents = image.get("agents", {})
    available = [
        agent_id for agent_id in held_agents if agent_id in owner.backup_agents
    ]
    if not available:
        raise ToolFailure(
            "RESOURCE_NOT_FOUND",
            "The target backup has no available agent copy",
            status="not_started",
        )
    agent_id = next(
        (
            agent_id
            for agent_id in available
            if agent_id.startswith(("hassio.", "backup."))
        ),
        available[0],
    )
    protected = held_agents[agent_id].get("protected")
    if not isinstance(protected, bool):
        raise ToolFailure(
            "CONFIG_VALIDATION_FAILED",
            "The selected backup copy's protection state is unknown",
            status="not_started",
        )
    agent_id, agent, password = access.credentials(agent_id, require_password=protected)
    if safety_backup:
        access.password()
    target = FullRestoreTarget(
        owner,
        owner._reader_writer,
        backup_id,
        agent_id,
        agent,
        password,
        copy.deepcopy(image),
        effective,
        tuple(addons),
        tuple(folders),
    )
    await target.verify(manager, actor)
    safety_id = None
    if safety_backup:
        # The target is pinned individually; safety legitimately changes the index.
        safety = await access.create(
            f"Before_restore_{backup_id}",
            parts={
                "include_homeassistant": True,
                "include_database": effective,
                "include_addons": addons,
                "include_folders": folders,
            },
        )
        safety_id = safety["backup_id"]
    try:
        await target.verify(manager, actor)
        manager.system_lifecycle.schedule(
            "backup_restore", actor, restore_target=target
        )
    except ToolFailure as error:
        failure = ToolFailure(
            error.code,
            str(error),
            data=error.data,
            status="confirmed" if safety_id is not None else error.status,
        )
        failure.data.update(
            safety_backup_id=safety_id,
            restore_requested=False,
            restore_completed=False,
        )
        raise failure from None
    warnings = [
        "Full restore was scheduled; Home Assistant may restart and the connection may be lost. Completion is not confirmed."
    ]
    return {
        "success": True,
        "backup_id": backup_id,
        "safety_backup_id": safety_id,
        "status": "Full backup restore requested",
        "operation_outcome": "scheduled",
        "restore_completed": False,
        "restore_database": effective,
        "restore_addons": addons,
        "restore_folders": folders,
        "warnings": warnings,
        "note": (
            "A safety backup of the selected restore parts completed on Core's default destination before scheduling."
            if safety_backup
            else "No safety backup was created (safety_backup=false)."
        ),
    }


def _restore_selection(
    access: NativeFullBackups,
    image: dict[str, Any],
    database: bool | None,
    addons: list[str] | None,
    folders: list[str] | None,
) -> tuple[bool, list[str], list[Any]]:
    """HA-MCP preflight prevents backups of requests Core rejects or silently omits.

    Core 2026.10.4 backup/manager.py and hassio/backup.py own execution;
    their reader/writers reject non-Supervisor parts and a mismatched database.
    """
    reasons = []
    held_database = image.get("database_included")
    if not isinstance(held_database, bool):
        reasons.append("The backup database inclusion state is unknown")
    effective = bool(held_database) if database is None else database
    if effective and held_database is False:
        reasons.append("restore_database=true but the backup contains no database")
    if access.supervised and held_database is True and not effective:
        reasons.append("Supervisor requires restore_database to match the backup")
    if image.get("homeassistant_included") is not True:
        reasons.append("The backup must contain Home Assistant configuration")
    addons = list(addons or [])
    folders = access.folders(folders) or []
    if not access.supervised and (addons or folders):
        reasons.append("restore_addons and restore_folders require Supervisor")
    held_addons = {app["slug"] for app in image.get("addons") or []}
    if missing := set(addons) - held_addons:
        reasons.append(f"The backup contains no apps: {', '.join(sorted(missing))}")
    if missing := set(folders) - set(image.get("folders") or []):
        reasons.append(f"The backup contains no folders: {', '.join(sorted(missing))}")
    if reasons:
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER", "; ".join(reasons), status="not_started"
        )
    return effective, addons, folders
