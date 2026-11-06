"""Resolve package exports without importing unused transport implementations."""

from collections.abc import Mapping
from importlib import import_module
from typing import Any


def resolve_export(
    module_name: str,
    namespace: dict[str, Any],
    exports: Mapping[str, tuple[str, str]],
    name: str,
) -> Any:
    """Import and cache one historical public name in its owning package."""
    if name not in exports:
        raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
    source, attribute = exports[name]
    value = getattr(import_module(source, module_name), attribute)
    namespace[name] = value
    return value
