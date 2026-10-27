"""Diagnostic field selection and process facts without a transport."""

import platform

_CONFIG_TOGGLE_FIELDS: tuple[str, ...] = (
    "enable_beta_features",
    "read_only_mode",
    "enable_tool_security_policies",
    "enable_websocket",
    "enable_dashboard_partial_tools",
    "enable_tool_search",
    "tool_search_max_results",
    "enable_yaml_config_editing",
    "enable_filesystem_tools",
    "enable_code_mode",
    "enabled_tool_modules",
    "enable_snapshot_actions",
    "backup_read_only",
)


def _detect_platform() -> dict[str, str]:
    """Detect platform information."""
    return {
        "os": platform.system(),  # Windows, Darwin, Linux
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
    }


__all__ = [
    "_CONFIG_TOGGLE_FIELDS",
]
