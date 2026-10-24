"""Full backups through Core's published manager and its selected reader/writer."""

import asyncio
import copy
import json
import time
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.backup_controls import _enforce_backup_controls
from ha_mcp.runtime.backup_values import (
    _check_snapshot_deletable,
    _parse_backup_date,
    _summarize_backup,
)
from ha_mcp.runtime.progress import report_progress
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .registry_access import admit_registry_write, authorize_actor


class NativeFullBackups:
    def __init__(self, manager: Any, actor: CallContext) -> None:
        self.manager, self.actor = manager, actor
        self.owner: Any = None

    def pin(self) -> Any:
        if "backup" not in self.manager.hass.config.components:
            raise UnsupportedOperation("Core backup integration is unavailable")
        core = import_module("homeassistant.components.backup")
        owner = core.async_get_manager(self.manager.hass)
        if owner is None or isinstance(owner, asyncio.Future):
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Core backup is still initializing",
                status="not_started",
            )
        if self.owner is None:
            self.owner = owner
        elif self.owner is not owner:
            raise ToolFailure(
                "CONFIG_CONFLICT", "Core backup manager changed", status="not_started"
            )
        return owner

    async def guard(self, action: str) -> Any:
        writes = action != "list"
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
            "snapshot",
            action,
            not writes,
        )
        return self.pin()

    async def inventory(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        owner = await self.guard("list")
        rows, errors = await await_owned_io(owner.async_get_backups())
        await self.guard("list")
        # Core's serializer is authoritative for dataclasses, enums and dates.
        values: list[dict[str, Any]] = json.loads(json_bytes(list(rows.values())))
        return values, {key: type(error).__name__ for key, error in errors.items()}

    async def list(self, limit: int) -> dict[str, Any]:
        rows, errors = await self.inventory()
        floor = datetime.min.replace(tzinfo=UTC)
        rows.sort(
            key=lambda row: _parse_backup_date(row.get("date")) or floor, reverse=True
        )
        result: dict[str, Any] = {
            "success": True,
            "count": min(limit, len(rows)),
            "total": len(rows),
            "backups": [{**row, **_summarize_backup(row)} for row in rows[:limit]],
        }
        if errors:
            result.update(
                agent_errors=errors,
                partial=True,
                warnings=[
                    "Some backup agents could not be read; this inventory is incomplete."
                ],
            )
        return result

    def credentials(self) -> tuple[str, Any, str]:
        from ..websocket_api.system import (
            _backup_default_password,
            _preferred_local_agent_id,
        )

        owner = self.pin()
        agent_id = _preferred_local_agent_id(owner.backup_agents)
        if agent_id is None:
            raise ToolFailure(
                "CONFIG_VALIDATION_FAILED",
                "No local backup agent is available",
                status="not_started",
            )
        password = _backup_default_password(owner)
        if not isinstance(password, str) or not password:
            raise ToolFailure(
                "CONFIG_VALIDATION_FAILED",
                "Configure Home Assistant's default backup password before creating or restoring a full backup",
                status="not_started",
            )
        return agent_id, owner.backup_agents[agent_id], password

    def idle(self) -> None:
        state = import_module(
            "homeassistant.components.backup.manager"
        ).BackupManagerState
        if self.pin().state is not state.IDLE:
            raise ToolFailure(
                "BACKUP_BUSY",
                "Core's backup manager is busy; no operation was dispatched",
                status="not_started",
            )

    async def create(self, name: str | None, *, safety: bool = False) -> dict[str, Any]:
        async with registry_update_lock("core_backup", "operations"):
            await self.guard("create")
            agent_id, agent, password = self.credentials()
            nonce = uuid4().hex
            name = name or f"MCP_Backup_{datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}"

            async def worker() -> dict[str, Any]:
                owner = await self.guard("create")
                current_id, current_agent, current_password = self.credentials()
                if (current_id, current_password) != (
                    agent_id,
                    password,
                ) or current_agent is not agent:
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "Backup destination or protection changed",
                        status="not_started",
                    )
                self.idle()
                started = time.monotonic()
                try:
                    # This original API waits for Core's finish task. The owned
                    # task is never cancelled just to satisfy a tool deadline.
                    job = await self._with_progress(
                        owner.async_create_backup(
                            agent_ids=[agent_id],
                            extra_metadata={"ha_mcp_creation_id": nonce},
                            include_addons=None,
                            include_all_addons=agent_id == "hassio.local",
                            include_database=safety,
                            include_folders=None,
                            include_homeassistant=True,
                            name=name,
                            password=password,
                        )
                    )
                    rows, errors = await self.inventory()
                    matches = [
                        row
                        for row in rows
                        if row.get("extra_metadata", {}).get("ha_mcp_creation_id")
                        == nonce
                    ]
                    if (
                        errors
                        or len(matches) != 1
                        or agent_id not in matches[0].get("agents", {})
                    ):
                        raise RuntimeError(
                            "Completed backup could not be identified on its local agent"
                        )
                    backup = matches[0]
                    if (
                        backup.get("failed_agent_ids")
                        or backup.get("failed_addons")
                        or backup.get("failed_folders")
                    ):
                        raise RuntimeError("Core reported an incomplete backup")
                    return {
                        "success": True,
                        "backup_id": backup["backup_id"],
                        "backup_job_id": job.backup_job_id,
                        "name": backup["name"],
                        "date": backup.get("date"),
                        "size_bytes": backup["agents"][agent_id].get("size"),
                        "status": "Backup completed successfully",
                        "duration_seconds": int(time.monotonic() - started),
                        "note": "Backup uses your Home Assistant's default backup password",
                        "backup": backup,
                    }
                except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - Core/agents can fail after creating an archive
                    raise ToolFailure(
                        "OUTCOME_UNKNOWN",
                        f"Core backup creation was dispatched ({type(error).__name__}); inspect backups before another create. No retry was performed.",
                        status="outcome_unknown",
                    ) from None

            return await await_owned_io(worker())

    async def _with_progress(self, operation: Any) -> Any:
        started = time.monotonic()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(10)
                await report_progress(
                    self.actor.progress,
                    progress=time.monotonic() - started,
                    message="Creating Home Assistant backup",
                )

        reporter = asyncio.create_task(heartbeat())
        try:
            # Enter Core immediately after admission, without a scheduling gap
            # for the writer or touching its shared finish task.
            return await operation
        finally:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)

    async def delete(self, backup_id: str, confirm: bool) -> dict[str, Any]:
        async with registry_update_lock("core_backup", "operations"):
            await self.guard("delete")
            if not self.manager.policy.flag("enable_snapshot_delete") or not confirm:
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Snapshot deletion requires enable_snapshot_delete=true and confirm=True",
                    status="not_started",
                )
            rows, errors = await self.inventory()
            if errors:
                raise ToolFailure(
                    "SERVICE_CALL_FAILED",
                    "Backup inventory is incomplete; deletion guards cannot be established",
                    status="not_started",
                )
            match = next(
                (row for row in rows if row.get("backup_id") == backup_id), None
            )
            if match is None:
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND", "Backup was not found", status="not_started"
                )
            minimum = int(self.manager.snapshots._value("snapshot_delete_min_age_days"))
            before = copy.deepcopy(match)
            agents = dict(self.owner.backup_agents)
            _check_snapshot_deletable(
                match, rows, minimum, backup_id, datetime.now(UTC)
            )

            async def worker() -> dict[str, Any]:
                owner = await self.guard("delete")
                fresh_rows, errors = await self.inventory()
                current = next(
                    (row for row in fresh_rows if row.get("backup_id") == backup_id),
                    None,
                )
                if errors or current is None or not json_values_equal(before, current):
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "Backup identity or inventory changed during deletion preparation",
                        status="not_started",
                    )
                await self.guard("delete")
                if set(owner.backup_agents) != set(agents) or any(
                    owner.backup_agents[key] is not agent
                    for key, agent in agents.items()
                ):
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "Backup agents changed during deletion preparation",
                        status="not_started",
                    )
                if not self.manager.policy.flag("enable_snapshot_delete"):
                    raise ToolFailure(
                        "READ_ONLY",
                        "Snapshot deletion was disabled",
                        status="not_started",
                    )
                _check_snapshot_deletable(
                    current,
                    fresh_rows,
                    int(self.manager.snapshots._value("snapshot_delete_min_age_days")),
                    backup_id,
                    datetime.now(UTC),
                )
                self.idle()
                try:
                    errors = await owner.async_delete_backup(backup_id)
                    if errors:
                        raise RuntimeError("Some agents did not confirm deletion")
                    await self.guard("list")
                    return {
                        "success": True,
                        "backup_id": backup_id,
                        "name": match.get("name"),
                        "status": "Backup deleted successfully",
                    }
                except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - an agent may have already deleted its archive
                    raise ToolFailure(
                        "OUTCOME_UNKNOWN",
                        f"Backup deletion was dispatched ({type(error).__name__}); inspect the inventory before retrying",
                        status="outcome_unknown",
                    ) from None

            return await await_owned_io(worker())
