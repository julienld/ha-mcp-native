"""Snapshot gates for service calls, with settings supplied by the entry owner."""

from ha_mcp.tool_api import ToolFailure

from .action_constants import _SNAPSHOT_SERVICE_ACTIONS


def check_service_policy(
    domain: str,
    service: str,
    *,
    enable_snapshot_actions: bool = False,
    backup_read_only: bool = True,
) -> None:
    action = _SNAPSHOT_SERVICE_ACTIONS.get(
        (domain.strip().lower(), service.strip().lower())
    )
    if action is None:
        return
    if not enable_snapshot_actions:
        raise ToolFailure(
            "CONFIG_VALIDATION_FAILED",
            "Full Home Assistant snapshot actions are disabled (enable_snapshot_actions=false).",
            status="not_started",
        )
    if backup_read_only:
        raise ToolFailure(
            "READ_ONLY_MODE",
            "Backup Read Only is enabled (backup_read_only=true).",
            status="not_started",
        )
