"""Bind a live File & YAML entry to the admitted native actor."""

from __future__ import annotations

from typing import Any

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure

from ..const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_TOOLS
from ..file_access import DATA_FILE_ACCESS, FileAccess
from .registry_access import authorize_actor


class NativeFileEntry:
    def __init__(
        self,
        manager: Any,
        actor: CallContext,
        *,
        feature: str = "enable_filesystem_tools",
        require_auto_backup: bool = True,
    ) -> None:
        self.manager, self.actor, self.feature = manager, actor, feature
        self.owner: FileAccess | None = None
        self.require_auto_backup = require_auto_backup

    async def guard(self, writes: bool) -> None:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        owner = self.manager.hass.data.get(DOMAIN, {}).get(DATA_FILE_ACCESS)
        if (
            not isinstance(owner, FileAccess)
            or not owner.active
            or owner.entry is None
            or owner.entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_TOOLS)
            != ENTRY_TYPE_TOOLS
            or self.manager.hass.config_entries.async_get_entry(owner.entry.entry_id)
            is not owner.entry
            or owner.entry.disabled_by is not None
        ):
            raise ToolFailure(
                "COMPONENT_NOT_INSTALLED",
                'The optional "HA-MCP File & YAML Tools" entry is not set up. '
                "Add this entry to the integration to use file and YAML tools.",
                status="not_started",
            )
        if self.owner is not None and owner is not self.owner:
            raise ToolFailure(
                "CONFIG_CONFLICT", "The File & YAML entry changed", status="not_started"
            )
        self.owner = owner
        if not self.manager.policy.flag(
            "enable_beta_features"
        ) or not self.manager.policy.flag(self.feature):
            raise ToolFailure(
                "FEATURE_DISABLED",
                "File/YAML capability is disabled",
                status="not_started",
            )
        if writes:
            check_mutation_admission(self.actor)
            if self.actor.read_only or self.manager.policy.read_only:
                raise ToolFailure(
                    "READ_ONLY", "File writes are disabled", status="not_started"
                )
            if self.require_auto_backup and not self.manager.policy.flag(
                "enable_auto_backup"
            ):
                raise ToolFailure(
                    "CONFIG_VALIDATION_FAILED",
                    "File writes require auto-backup. Enable it in the Backups settings before retrying; nothing was changed.",
                    status="not_started",
                )

    async def _owner(self, writes: bool = False) -> FileAccess:
        await self.guard(writes)
        assert self.owner is not None
        return self.owner
