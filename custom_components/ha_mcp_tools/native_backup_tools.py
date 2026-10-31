"""Bind the single backup contract to Core and native snapshot owners."""

from dataclasses import replace
from typing import Any

from ha_mcp.runtime.backup_hints import BACKUP_HINTS
from ha_mcp.runtime.backup_limits import BACKUP_MAX_WAIT_S, SAFETY_BACKUP_MAX_WAIT_S
from ha_mcp.runtime.backup_tools import BackupFacade, backup_description
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.backups import NativeBackups
from .native_server_policy import setting_value


def _operation_timeout(arguments: dict[str, Any]) -> float | None:
    if arguments.get("scope") == "snapshot":
        if arguments.get("action") == "create":
            return BACKUP_MAX_WAIT_S + 120
        if arguments.get("action") == "restore":
            return SAFETY_BACKUP_MAX_WAIT_S + 120
    return None


def backup_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> Any:
        result = await BackupFacade(
            NativeBackups(manager, actor).manage
        ).ha_manage_backup(**arguments)
        status = result.get("operation_outcome")
        return ToolResult(result, status=status) if status is not None else result

    definition = define_tool(
        manager.backend,
        BackupFacade,
        "ha_manage_backup",
        handler=invoke,
        source_module="backup",
        tags=frozenset({"System"}),
        annotations=write_hints(
            "Manage Backups", destructive=True, idempotent=False, open_world=False
        ),
        meta={
            "ha_mcp": {
                "native_support": {
                    "note": "Full archives use Core's configured backup manager. A full restore requires a completed safety backup, is unavailable under hidden-entity enforcement, and reports only a scheduled request; it does not confirm restart or restore completion. Edit backups reuse the existing snapshot store and domain restorers."
                }
            }
        },
    )
    flags = getattr(getattr(manager, "policy", None), "flags", {})
    hint = str(setting_value(flags, "backup_hint")).lower()
    return replace(
        definition,
        description=backup_description(BACKUP_HINTS.get(hint, BACKUP_HINTS["normal"])),
        operation_timeout=_operation_timeout,
    )
