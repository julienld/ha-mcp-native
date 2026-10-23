"""Custom tools for the Home Assistant MCP server."""

from typing import Any

from .._lazy_exports import resolve_export

_LAZY_EXPORTS = {
    "DeviceControlTools": (".device_control", "DeviceControlTools"),
    "create_device_control_tools": (".device_control", "create_device_control_tools"),
    "SmartSearchTools": (".smart_search", "SmartSearchTools"),
    "create_smart_search_tools": (".smart_search", "create_smart_search_tools"),
}


def __getattr__(name: str) -> Any:
    return resolve_export(__name__, globals(), _LAZY_EXPORTS, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EXPORTS.keys())


__all__ = [
    "DeviceControlTools",
    "SmartSearchTools",
    "create_device_control_tools",
    "create_smart_search_tools",
]
