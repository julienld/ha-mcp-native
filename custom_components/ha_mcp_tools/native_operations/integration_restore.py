"""Restore an existing integration's enabled state through Core's original owner."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
)
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io

from .integration_state import NativeIntegrationState
from .registry_access import authorize_actor


@dataclass(slots=True)
class NativeIntegrationRestore:
    manager: Any
    actor: CallContext
    data: dict[str, Any]
    entry: Any = field(default=None, init=False, repr=False)

    async def current(self) -> dict[str, Any] | None:
        config = self.data["config"]
        identifier = self.data["entity_id"]
        if (
            not isinstance(config, dict)
            or config.get("entry_id") != identifier
            or {"data", "options"}.intersection(config)
        ):
            raise InvalidBackupSnapshotError(
                "Integration snapshots contain only their matching public metadata"
            )
        await authorize_actor(self.actor, self.manager.backend.authorize)
        self.entry = self.manager.hass.config_entries.async_get_entry(identifier)
        return (
            json.loads(json_bytes(self.entry.as_json_fragment))
            if self.entry is not None
            else None
        )

    async def apply(self, before: Any) -> dict[str, Any]:
        if before is None or self.entry is None:
            raise BackupRestoreError(
                "The integration no longer exists; metadata cannot recreate it. Add the integration again through its Core flow.",
                reason="entry_deleted",
                apply_status="not_applied",
            )
        return await await_owned_io(self._apply(before))

    comparison = current

    async def _apply(self, before: dict[str, Any]) -> dict[str, Any]:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        identifier = self.data["entity_id"]
        if self.manager.hass.config_entries.async_get_entry(
            identifier
        ) is not self.entry or not json_values_equal(
            json.loads(json_bytes(self.entry.as_json_fragment)), before
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Integration changed during the safety capture",
                status="not_started",
            )
        owner = NativeIntegrationState(
            self.manager.hass, self.manager.backend.authorize
        )
        enabled = self.data["config"].get("disabled_by") is None
        try:
            result = await owner._enabled(
                {
                    "entry_id": identifier,
                    "enabled": enabled,
                    "expected_entry": before,
                    "expected_owner": self.entry,
                },
                self.actor,
            )
        except asyncio.CancelledError as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Core integration restoration was interrupted after dispatch",
                status="outcome_unknown",
            ) from error
        try:
            await authorize_actor(self.actor, self.manager.backend.authorize)
        except ToolFailure as error:
            raise ToolFailure(
                error.code,
                "Integration restoration was dispatched but its result is no longer authorized",
                status="outcome_unknown",
            ) from None
        current = self.manager.hass.config_entries.async_get_entry(identifier)
        if current is not self.entry or (current.disabled_by is None) != enabled:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Core did not retain the requested integration state; inspect it before retrying",
                status="outcome_unknown",
            )
        return {
            **result,
            "warnings": [
                "Integration snapshots restore only the enabled state of an existing entry. Credentials and connection options are not stored or replayed."
            ],
        }
