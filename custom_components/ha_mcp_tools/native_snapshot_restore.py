"""Native domain operations for the common snapshot restore coordinator."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

from ha_mcp.runtime.helper_schema import SIMPLE_HELPER_TYPES
from ha_mcp.runtime.mutation_guard import current_admission
from ha_mcp.runtime.snapshot_files import _is_flow_helper_domain
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
    MandatoryBackupError,
)
from ha_mcp.runtime.snapshot_restore import ApplyStatus
from ha_mcp.runtime.snapshot_store import SnapshotStore
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .native_snapshot_domains import StorageBefore


@contextmanager
def restore_controls(snapshots: Any, actor: CallContext) -> Iterator[None]:
    """Recheck backup controls at every nested mutation without redoing approval."""
    admission = current_admission.get()
    if admission is None:
        yield
        return

    def check() -> None:
        admission.check_policy()
        snapshots._check_restore_controls(actor)

    token = current_admission.set(replace(admission, check_policy=check))
    try:
        yield
    finally:
        current_admission.reset(token)


@dataclass
class NativeRestoreOperations:
    snapshots: Any
    data: dict[str, Any]
    actor: CallContext
    store: SnapshotStore
    before: Any = field(default=None, init=False)
    revision: float | None = field(default=None, init=False)
    storage: StorageBefore | None = field(default=None, init=False)
    calendar_owner: Any = field(default=None, init=False)
    resource_owner: Any = field(default=None, init=False)

    async def needs_safety(self) -> bool:
        domain = self.data["domain"]
        if domain == "dashboard_resource":
            self.resource_owner = self.snapshots.domains.resources(self.actor)
            self.before = await self.resource_owner.read_snapshot(
                self.data["entity_id"], self.data["config"]
            )
            return self.before is not None and not self.resource_owner.snapshot_matches(
                self.before, self.data["config"]
            )
        if domain == "calendar_event":
            self.calendar_owner = self.snapshots.domains.calendar_restore(
                self.data, self.actor
            )
            self.before = await self.calendar_owner.current()
            return self.before is not None
        if _is_flow_helper_domain(domain) or domain == "helper_config_subentry":
            self.before = await self.snapshots.domains.current(self.data, self.actor)
            self.revision = self.snapshots.domains.entry_revision(self.data)
            return self.before is not None or domain == "helper_config_subentry"
        if domain in {
            "automation",
            "script",
            "scene",
            "todo_item",
            "category",
            "area_or_floor",
            "label",
            "device",
        }:
            self.before = await self.snapshots.domains.current(self.data, self.actor)
            return self.before is not None
        helper_type = "zone" if domain == "zone" else domain.removeprefix("helper_")
        if (
            domain not in {"zone", f"helper_{helper_type}"}
            or helper_type not in SIMPLE_HELPER_TYPES
        ):
            raise UnsupportedOperation(f"snapshots.{domain}.restore")
        config = self.data["config"]
        if (
            not isinstance(config, dict)
            or not isinstance(config.get("id"), str)
            or not config["id"]
        ):
            raise InvalidBackupSnapshotError(
                "Stored helper snapshot has no item identity"
            )
        # A renamed entity alias must never select another stored item for the
        # recovery point. The restore writes the original stored ID.
        target = {**self.data, "entity_id": config["id"]}
        self.storage = await self.snapshots.domains.storage_before(target, self.actor)
        self.before = self.storage.snapshot
        if self.before is None and helper_type == "zone":
            return False
        if self.before is None:
            raise ToolFailure(
                "RESOURCE_NOT_FOUND",
                "Stored helper no longer exists",
                status="not_started",
            )
        return True

    async def capture_safety(self, outcome: dict[str, Any]) -> Path | None:
        flow = (
            _is_flow_helper_domain(self.data["domain"])
            or self.data["domain"] == "helper_config_subentry"
        )
        if self.before is None or (
            not flow and not self.snapshots.manager.policy.flag("enable_auto_backup")
        ):
            return None
        store = self.store
        before = copy.deepcopy(self.before)

        def capture() -> Path | None:
            from homeassistant.helpers.json import json_bytes

            return store.capture(
                self.data["domain"],
                self.data["entity_id"],
                json.loads(json_bytes(before)),
                "ha_manage_backup.restore.safety",
                force=True,
            )

        try:
            path: Path | None = await self.snapshots._io(capture)
        except OSError as error:
            raise MandatoryBackupError(
                "The safety snapshot could not be captured"
            ) from error
        if path is None:
            raise MandatoryBackupError("The safety snapshot could not be captured")
        return path

    async def require_safety(self, path: Path) -> None:
        if not await self.snapshots._io(path.is_file):
            raise MandatoryBackupError(
                "The safety snapshot disappeared before restore",
                safe_detail="safety snapshot disappeared",
            )

    async def apply(self, needs_safety: bool, has_safety: bool) -> Any:
        self.snapshots._require_restore_access(self.actor)
        if self.data["domain"] == "dashboard_resource":
            assert self.resource_owner is not None
            return await self.resource_owner.restore_snapshot(
                self.data["entity_id"], self.data["config"], self.before
            )
        if self.data["domain"] == "calendar_event":
            assert self.calendar_owner is not None
            return await self.calendar_owner.apply(self.before)
        if (
            _is_flow_helper_domain(self.data["domain"])
            or self.data["domain"] == "helper_config_subentry"
        ):
            return await self.snapshots.domains.apply_flow(
                self.data,
                self.actor,
                needs_safety=needs_safety,
                has_safety=has_safety,
                revision=self.revision,
            )
        if self.data["domain"] in {"automation", "script", "scene"}:
            return await self.snapshots.domains.apply_config(
                self.data, self.before, self.actor
            )
        if self.data["domain"] == "label":
            return await self.snapshots.domains.restore_label(
                self.data, self.before, self.actor
            )
        if self.data["domain"] == "device":
            return await self.snapshots.domains.restore_device(
                self.data, self.before, self.actor
            )
        if self.data["domain"] in {"category", "area_or_floor"}:
            from ha_mcp.runtime.snapshot_registry_restore import restore_registry

            return await restore_registry(
                self.snapshots.manager.backend,
                self.actor,
                self.data["domain"],
                self.data["entity_id"],
                self.data["config"],
                self.before,
            )
        if self.data["domain"] == "todo_item":
            from ha_mcp.runtime.snapshot_todo import restore_todo

            return await restore_todo(
                self.snapshots.manager.backend,
                self.actor,
                self.data["entity_id"],
                self.data["config"],
                self.before,
            )
        assert self.storage is not None
        return await self.snapshots.domains.apply_storage(
            self.data, self.storage, self.actor
        )

    def log_failure(self, step: str, error: BaseException) -> None:
        # Storage restore failures are mapped without logging configuration.
        return None

    def failed(
        self,
        error: BaseException,
        step: str,
        status: ApplyStatus,
        outcome: dict[str, Any],
    ) -> NoReturn:
        if (
            isinstance(error, ToolFailure)
            and isinstance(error.data, dict)
            and error.data.get("config_applied") is True
        ):
            status = "applied"
        elif (isinstance(error, ToolFailure) and error.status == "outcome_unknown") or (
            self.resource_owner is not None
            and self.resource_owner.initialization_started
        ):
            status = "unknown"
        elif (
            isinstance(error, (ToolFailure, UnsupportedOperation))
            and error.status == "not_started"
        ):
            status = "not_applied"
        message = (
            str(error)
            if isinstance(error, ToolFailure) and error.status == "not_started"
            else "The snapshot could not be restored; inspect the recorded outcome before retrying"
        )
        if (
            self.resource_owner is not None
            and self.resource_owner.initialization_started
        ):
            outcome = {**outcome, "resource_initialization_started": True}
        raise BackupRestoreError(
            message,
            apply_status=status,
            reason="backup_capture_failed"
            if isinstance(error, MandatoryBackupError)
            else "restore_failed",
            **outcome,
        ) from error

    def completed(self, result: Any) -> dict[str, Any]:
        extra = {}
        if (
            _is_flow_helper_domain(self.data["domain"])
            or self.data["domain"] == "helper_config_subentry"
        ):
            extra["verification_status"] = "matched"
            if isinstance(result, dict) and result.get("restore_mode") == "recreated":
                extra.update(
                    {
                        key: result[key]
                        for key in ("restore_mode", "entity_id", "original_entry_id")
                        if key in result
                    }
                )
        return {"apply_status": "applied", **extra}
