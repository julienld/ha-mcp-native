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
                    "note": "Full archives use Core's backup manager and its registered agents. Automatic creation honors every configured destination and its protection settings; explicit include_* selections use Core's default destination, including the Supervisor default backup mount. Creation requires readback from every selected agent. Restore pins an available copy and its protection before scheduling, preferring Core or Supervisor copies. Listing reads the configured backup-agent inventory. Deletion follows Core's backup-ID semantics across registered agents and requires complete inventory. A full restore is unavailable under hidden-entity enforcement and reports only a scheduled request; it does not confirm restart or restore completion. safety_backup=true requires a completed backup of the selected restore parts on Core's default destination before scheduling. Edit backups reuse the existing snapshot store and domain restorers."
                }
            }
        },
    )
    return replace(
        definition,
        description=backup_description(BACKUP_HINTS["normal"]),
        operation_timeout=_operation_timeout,
    )
