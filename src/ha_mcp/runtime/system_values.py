"""Named reload services and public result values, independent of transport."""

from typing import Any

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
