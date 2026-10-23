"""Pin a full-backup target through safety capture and deferred Core handoff."""

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.backup_values import _backup_protected
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io

from .full_backups import NativeFullBackups


@dataclass(frozen=True, slots=True)
class FullRestoreTarget:
    owner: Any = field(repr=False)
    reader_writer: Any = field(repr=False)
    backup_id: str
    agent_id: str
    agent: Any = field(repr=False)
    password: str = field(repr=False)
    image: dict[str, Any] = field(repr=False)
    restore_database: bool

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
        agent_id, agent, password = access.credentials()
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
            "password": self.password if _backup_protected(self.image) else None,
            "restore_addons": [],
            "restore_folders": [],
            "restore_homeassistant": True,
            "restore_database": self.restore_database,
        }


async def schedule_full_restore(
    manager: Any, actor: CallContext, backup_id: str, restore_database: bool
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
        raise ToolFailure(
            "RESOURCE_NOT_FOUND", "Target backup not found", status="not_started"
        )
    image = json.loads(json_bytes(row))
    agent_id, agent, password = access.credentials()
    if agent_id not in image.get("agents", {}):
        raise ToolFailure(
            "RESOURCE_NOT_FOUND",
            "The target backup is not available on the local agent",
            status="not_started",
        )
    protected = _backup_protected(image)
    if protected is None:
        raise ToolFailure(
            "CONFIG_VALIDATION_FAILED",
            "The backup protection state is unknown",
            status="not_started",
        )
    effective = (
        image.get("database_included")
        if agent_id == "hassio.local"
        else restore_database
    )
    if effective is None:
        effective = restore_database
    target = FullRestoreTarget(
        owner,
        owner._reader_writer,
        backup_id,
        agent_id,
        agent,
        password,
        copy.deepcopy(image),
        effective,
    )
    await target.verify(manager, actor)
    # A failed or unavailable safety snapshot blocks full restore. The target
    # is pinned individually; creating safety legitimately changes the index.
    safety = await access.create(f"Before_restore_{backup_id}", safety=True)
    try:
        await target.verify(manager, actor)
        manager.system_lifecycle.schedule(
            "backup_restore", actor, restore_target=target
        )
    except ToolFailure as error:
        failure = ToolFailure(error.code, str(error), status="confirmed")
        failure.data.update(
            safety_backup_id=safety["backup_id"],
            restore_requested=False,
            restore_completed=False,
        )
        raise failure from None
    warnings = [
        "Full restore was scheduled; Home Assistant may restart and the connection may be lost. Completion is not confirmed."
    ]
    if effective != restore_database:
        warnings.append(
            f"restore_database was adjusted to {effective} to match the target archive, as required by Supervisor."
        )
    return {
        "success": True,
        "backup_id": backup_id,
        "safety_backup_id": safety["backup_id"],
        "status": "Full backup restore requested",
        "operation_outcome": "scheduled",
        "restore_completed": False,
        "restore_database": effective,
        "warnings": warnings,
        "note": "A full safety backup completed before the restore request was scheduled.",
    }
