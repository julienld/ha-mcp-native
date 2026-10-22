"""Custom tools for the Home Assistant MCP server."""

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "DeviceControlTools": (".device_control", "DeviceControlTools"),
    "create_device_control_tools": (".device_control", "create_device_control_tools"),
    "SmartSearchTools": (".smart_search", "SmartSearchTools"),
    "create_smart_search_tools": (".smart_search", "create_smart_search_tools"),
}


def __getattr__(name: str) -> Any:
    """Load tool families only when a caller requests them."""
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EXPORTS.keys())

__all__ = [
    "DeviceControlTools",
    "SmartSearchTools",
    "create_device_control_tools",
    "create_smart_search_tools",
]
