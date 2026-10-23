"""Restore dashboard bodies and settings through their original Core owners."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.dashboard_snapshots import (
    dashboard_snapshot,
    valid_dashboard_capture,
)
from ha_mcp.runtime.snapshot_files import _DASHBOARD_KIND
from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
)
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .command_validation import _validated_payload
from .dashboard_storage import DashboardState, DashboardStorage


def stored_dashboard(data: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    if data.get("kind") == _DASHBOARD_KIND and not valid_dashboard_capture(
        data["config"]
    ):
        raise InvalidBackupSnapshotError(
            "Dashboard snapshot has invalid settings/config values"
        )
    snapshot, legacy = dashboard_snapshot(data)
    row, config = snapshot["dashboard"], snapshot["config"]
    if config is not None and not isinstance(config, dict):
        raise InvalidBackupSnapshotError(
            "Dashboard configuration must be an object or null"
        )
    target = data["entity_id"]
    if row is not None:
        accepted = {row["url_path"], row.get("id")}
        if row["url_path"] == "lovelace":
            accepted.add("default")
        if target not in accepted:
            raise InvalidBackupSnapshotError(
                "Dashboard settings do not match the snapshot identity"
            )
        target = row["url_path"]
    return "lovelace" if target == "default" else target, snapshot, legacy


@dataclass(slots=True)
class NativeDashboardRestore:
    storage: DashboardStorage
    data: dict[str, Any]
    state: DashboardState | None = field(default=None, init=False)

    async def current(self) -> dict[str, Any] | None:
        target, _saved, _legacy = stored_dashboard(self.data)
        self.state = await self.storage.read(target)
        return self.state.snapshot

    async def comparison(self) -> Any:
        _target, _saved, legacy = stored_dashboard(self.data)
        current = await self.current()
        return current.get("config") if legacy and current is not None else current

    async def apply(self, before: Any) -> dict[str, Any]:
        target, saved, legacy = stored_dashboard(self.data)
        assert self.state is not None
        try:
            async with registry_update_lock("dashboard", target):
                if not json_values_equal(self.state.snapshot, before):
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "Dashboard restore no longer holds the safety before-image",
                        status="not_started",
                    )
                return await self._apply(saved, legacy)
        except BackupRestoreError:
            raise
        except Exception as error:
            raise self.storage.failure(error) from error

    async def _apply(self, saved: dict[str, Any], legacy: bool) -> dict[str, Any]:
        assert self.state is not None
        state = self.state
        row, config = saved["dashboard"], saved["config"]
        fields = None
        if row is not None and state.row is not None:
            fields = {
                key: value
                for key, value in row.items()
                if key not in {"id", "mode", "url_path"}
            }
            fields.setdefault("icon", None)
            # Ask the installed validator before applying the body, so an old
            # snapshot cannot leave a half-restored body on a known field error.
            _validated_payload(
                self.storage.hass,
                "lovelace/dashboards/update",
                {"dashboard_id": state.row["id"], **fields},
            )
        state, note = await self._ensure_owner(state, row, legacy)
        if config is None:
            state = await self.storage.clear(state)
        else:
            state = await self.storage.save(state, copy.deepcopy(config))
        if not state.verified:
            raise BackupRestoreError(
                "The dashboard save completed but its restored config could not be verified. Inspect the current dashboard before retrying.",
                apply_status="unknown",
                reason="verification_failed",
                config_applied=True,
                warnings=list(state.warnings),
            )
        if fields is not None:
            try:
                state = await self.storage.update_metadata(state, fields)
            except Exception as error:
                raise BackupRestoreError(
                    "The dashboard config was restored, but its settings may not have been restored. Inspect the dashboard before retrying.",
                    apply_status="unknown",
                    reason="settings_not_restored",
                    config_applied=True,
                ) from error
        result: dict[str, Any] = {
            "url_path": state.url_path,
            "dashboard_id": state.row.get("id") if state.row else None,
            "post_write_verified": state.verified,
        }
        if state.warnings:
            result["warnings"] = list(state.warnings)
        if note:
            result["note"] = note
        return result

    async def _ensure_owner(
        self, state: DashboardState, row: dict[str, Any] | None, legacy: bool
    ) -> tuple[DashboardState, str | None]:
        note = None
        if state.owner is None:
            if row is None or legacy:
                raise BackupRestoreError(
                    "This body-only snapshot cannot recreate a deleted dashboard without its settings.",
                    reason="entry_deleted",
                )
            values = {
                key: value
                for key, value in row.items()
                if key != "id" and value is not None
            }
            values.pop("url_path", None)
            if "-" not in state.url_path:
                values["allow_single_word"] = True
            state = await self.storage.create(state, values)
        elif row is not None and state.row is None:
            if state.url_path != "lovelace":
                raise BackupRestoreError(
                    "The saved storage dashboard is now owned by another dashboard mode.",
                    reason="dashboard_owner_changed",
                )
            note = "The config was restored to Home Assistant's default dashboard store, shown at /lovelace. Its saved title and icon are not restored; Core may migrate this store into a dashboard at the next restart."
        return state, note
