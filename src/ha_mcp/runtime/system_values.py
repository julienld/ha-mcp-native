"""Named reload services and public result values, independent of transport."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

RELOAD_TARGETS = {
    "all": None,  # Special case - reload all
    "automations": ("automation", "reload"),
    "scripts": ("script", "reload"),
    "scenes": ("scene", "reload"),
    "groups": ("group", "reload"),
    "input_booleans": ("input_boolean", "reload"),
    "input_numbers": ("input_number", "reload"),
    "input_texts": ("input_text", "reload"),
    "input_selects": ("input_select", "reload"),
    "input_datetimes": ("input_datetime", "reload"),
    "input_buttons": ("input_button", "reload"),
    "timers": ("timer", "reload"),
    "templates": ("template", "reload"),
    "persons": ("person", "reload"),
    "zones": ("zone", "reload"),
    "core": ("homeassistant", "reload_core_config"),
    "themes": ("frontend", "reload_themes"),
}


def reload_target_result(target: str, domain: str, service: str) -> dict[str, Any]:
    return {
        "success": True,
        "message": f"Successfully reloaded {target}",
        "target": target,
        "service": f"{domain}.{service}",
    }


def reload_entry_result(entry_id: str, require_restart: bool) -> dict[str, Any]:
    if require_restart:
        return {
            "success": True,
            "message": f"Config entry {entry_id} cannot be hot-reloaded; a Home Assistant restart is required for changes to take effect",
            "reloaded": False,
            "require_restart": True,
            "entry_id": entry_id,
        }
    return {
        "success": True,
        "message": f"Reloaded config entry {entry_id}",
        "reloaded": True,
        "require_restart": False,
        "entry_id": entry_id,
    }


def reload_all_result(results: list[str], errors: list[str]) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": True,
        "message": f"Reloaded {len(results)} components",
        "reloaded": results,
    }
    if errors:
        response["warnings"] = errors
    return response


def restart_scheduled_result() -> dict[str, Any]:
    return {
        "success": True,
        "requested": True,
        "scheduled": True,
        "message": "Home Assistant restart scheduled. This MCP server stops with Home Assistant; completion cannot be confirmed by this invocation.",
        "warnings": [
            "The request is revalidated before Core receives it. Calls may fail while Home Assistant stops and starts; reconnect after it becomes available."
        ],
    }


def restart_precheck_warnings(
    recorder: dict[str, Any] | None, backup: dict[str, Any] | None, *, safe_mode: bool
) -> list[str]:
    """Refuse what Core would refuse; return the warnings for the reply."""
    warnings = [
        "Calls may fail while this server starts. Try again after a few seconds."
    ]
    if safe_mode:
        warnings.append(
            "Safe mode loads no custom integrations, and this server is "
            "one: it stays down until Home Assistant is next "
            "restarted normally."
        )
    # A missing field (older Core, renamed key) means the check was not made,
    # so it warns like an unanswered command instead of passing silently.
    if recorder is None or not isinstance(recorder.get("migration_in_progress"), bool):
        warnings.append(
            "Could not read the recorder status to check for a database "
            "upgrade; Home Assistant refuses to restart during one."
        )
    elif recorder.get("migration_in_progress"):
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_LOCKED,
                "A database upgrade is in progress - restart refused",
                details=(
                    "Home Assistant cannot restart while the recorder "
                    "is upgrading its database."
                ),
                context={"recorder": recorder, "execution_status": "not_started"},
                suggestions=[
                    "Wait for the upgrade to finish, then call "
                    "ha_restart(confirm=True) again"
                ],
            )
        )
    state = backup.get("state") if backup else None
    if not isinstance(state, str):
        warnings.append(
            "Could not read the backup status to check for a running "
            "backup; Home Assistant cannot restart during one."
        )
    elif state != "idle":
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_LOCKED,
                f"Backup manager is busy ({state}) - restart refused",
                details=(
                    "Home Assistant cannot restart while the backup manager is "
                    "busy creating, receiving or restoring one."
                ),
                context={"backup_state": state, "execution_status": "not_started"},
                suggestions=[
                    "Wait for the backup to finish, then call "
                    "ha_restart(confirm=True) again"
                ],
            )
        )
    return warnings
