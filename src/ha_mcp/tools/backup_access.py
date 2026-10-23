"""Argument validation and access checks for explicit backup tool actions."""

import json
import posixpath
import urllib.parse
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backends.legacy import legacy_errors_sync
from ha_mcp.runtime import backup_controls as _backup_controls

from ..config import Settings, get_global_settings
from ..errors import ErrorCode, create_error_response
from ..runtime.action_constants import (
    _SNAPSHOT_SERVICE_ACTIONS,
)
from .helpers import raise_tool_error

gate_backup_combo = legacy_errors_sync(_backup_controls.gate_backup_combo)


require_backup_param = legacy_errors_sync(_backup_controls.require_backup_param)


def require_backup_access(settings: Settings, scope: str, action: str) -> None:
    """Gate explicit calls, including proxy dispatch, before any HA I/O.

    Automatic capture does not pass through this guard. The global/request
    read-only restriction takes precedence over the backup-specific controls.
    """
    from ..read_only import READ_ONLY_EXEMPT_TOOLS, require_write_access

    read_operation = (
        READ_ONLY_EXEMPT_TOOLS["ha_manage_backup"].blocked_write(
            {"scope": scope, "action": action}
        )
        is None
    )
    if not read_operation:
        require_write_access("ha_manage_backup")
    _enforce_backup_controls(settings, scope, action, read_operation)


_enforce_backup_controls = legacy_errors_sync(_backup_controls._enforce_backup_controls)


# Generic routes (ha_call_service and the Code Mode bridges) that perform the
# same full-snapshot operations as ha_manage_backup(scope="snapshot"), mapped to
# the equivalent action so the backup controls cannot be bypassed through them.
_SNAPSHOT_WS_ACTIONS: dict[str, str] = {
    "backup/details": "list",
    "backup/info": "list",
    "backup/generate": "create",
    "backup/generate_with_automatic_settings": "create",
    "backup/restore": "restore",
    "backup/delete": "delete",
}


def _normalized_path(path: str) -> str:
    """Fold percent-escapes, ``//``, ``.`` and ``..`` so an equivalent spelling
    cannot slip past (HTTP clients decode ``%62ackups`` to ``backups``).

    Leading slashes are stripped first: POSIX normpath keeps exactly two.
    """
    decoded = urllib.parse.unquote(path.split("?", 1)[0])
    stripped = decoded.strip().lower().lstrip("/")
    return posixpath.normpath("/" + stripped)


def _supervisor_backup_action(endpoint: Any, method: Any) -> str | None:
    """Classify a request against Supervisor's ``/backups`` API."""
    if not isinstance(endpoint, str):
        return None
    path = _normalized_path(endpoint)
    if path != "/backups" and not path.startswith("/backups/"):
        return None
    verb = str(method).strip().lower()
    if verb == "get":
        return "list"
    if verb == "delete":
        return "delete"
    return "restore" if "/restore" in path else "create"


def _service_action(domain: Any, service: Any) -> str | None:
    key = (str(domain or "").strip().lower(), str(service or "").strip().lower())
    return _SNAPSHOT_SERVICE_ACTIONS.get(key)


def _snapshot_route_action(
    domain: str | None,
    service: str | None,
    ws_command: str | None,
    ws_params: dict[str, Any] | None,
    rest_path: str | None,
) -> str | None:
    if rest_path is not None:
        parts = _normalized_path(rest_path).split("/")
        if len(parts) == 4 and parts[1] == "services":
            return _service_action(parts[2], parts[3])
        if len(parts) > 2 and parts[1] == "hassio":
            return _supervisor_backup_action("/".join(parts[2:]), "post")
        return None
    if ws_command is None:
        return _service_action(domain, service)
    command = ws_command.strip().lower()
    params = ws_params or {}
    if command == "call_service":
        return _service_action(params.get("domain"), params.get("service"))
    if command == "supervisor/api":
        return _supervisor_backup_action(params.get("endpoint"), params.get("method"))
    return _SNAPSHOT_WS_ACTIONS.get(command)


def _require_snapshot_route_access(action: str | None) -> None:
    if action is None:
        return
    if action == "delete":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Full Home Assistant snapshot deletion is only available through "
                "ha_manage_backup.",
                context={"scope": "snapshot", "action": action},
                suggestions=[
                    "Use ha_manage_backup(scope='snapshot', action='delete') so "
                    "enable_snapshot_delete and its protections apply."
                ],
            )
        )
    _enforce_backup_controls(
        get_global_settings(), "snapshot", action, action == "list"
    )


def guard_snapshot_route(
    *,
    domain: str | None = None,
    service: str | None = None,
    ws_command: str | None = None,
    ws_params: dict[str, Any] | None = None,
    rest_path: str | None = None,
) -> None:
    """Apply the snapshot backup controls to a generic service, WS or REST call.

    Deletion is always refused here: enable_snapshot_delete and the
    newest/scheduled/age protections only run inside ha_manage_backup.
    """
    _require_snapshot_route_access(
        _snapshot_route_action(domain, service, ws_command, ws_params, rest_path)
    )


def snapshot_route_refusal(
    *,
    ws_command: str | None = None,
    ws_params: dict[str, Any] | None = None,
    rest_path: str | None = None,
) -> str | None:
    """Return the snapshot-control refusal as text for the Code Mode bridges."""
    try:
        _require_snapshot_route_access(
            _snapshot_route_action(None, None, ws_command, ws_params, rest_path)
        )
    except ToolError as exc:
        error = json.loads(str(exc))["error"]
        suggestions = error.get("suggestions") or [error.get("suggestion", "")]
        return " ".join([error["message"], *suggestions]).strip()
    return None


__all__ = [
    "gate_backup_combo",
    "guard_snapshot_route",
    "require_backup_access",
    "require_backup_param",
    "snapshot_route_refusal",
]
