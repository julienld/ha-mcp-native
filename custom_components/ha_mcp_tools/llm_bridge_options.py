"""Independent Core LLM bridges and migration of explicit earlier choices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    DEFAULT_ENABLE_LLM_API,
    DOMAIN,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_NATIVE_LLM_IMPORT,
    OPT_LLM_API_EXPOSURE,
    OPT_NATIVE_IMPORT_APIS,
)


def export_enabled(options: Mapping[str, Any]) -> bool:
    return options.get(OPT_ENABLE_LLM_API, DEFAULT_ENABLE_LLM_API) is True


def _api_ids(options: Mapping[str, Any]) -> frozenset[str]:
    raw = options.get(OPT_NATIVE_IMPORT_APIS, [])
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(
        item
        for item in raw
        if isinstance(item, str) and item and not item.startswith(DOMAIN)
    )


def import_enabled(options: Mapping[str, Any]) -> bool:
    # Before the independent switch existed, saving API IDs was the opt-in.
    if OPT_ENABLE_NATIVE_LLM_IMPORT not in options:
        return bool(_api_ids(options))
    return options[OPT_ENABLE_NATIVE_LLM_IMPORT] is True


def selected_import_apis(options: Mapping[str, Any]) -> frozenset[str]:
    return _api_ids(options) if import_enabled(options) else frozenset()


def bridge_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Persist defaults once without resetting either explicit bridge choice."""
    return {
        **options,
        OPT_ENABLE_LLM_API: export_enabled(options),
        OPT_ENABLE_NATIVE_LLM_IMPORT: import_enabled(options),
    }


def submitted_options(
    saved: Mapping[str, Any], submitted: Mapping[str, Any]
) -> dict[str, Any]:
    """Partial options updates retain selections; an explicit empty list clears."""
    keys = (
        OPT_ENABLE_LLM_API,
        OPT_ENABLE_NATIVE_LLM_IMPORT,
        OPT_NATIVE_IMPORT_APIS,
        OPT_LLM_API_EXPOSURE,
    )
    return bridge_options(
        {**{key: saved[key] for key in keys if key in saved}, **submitted}
    )


def require_export_enabled(options: Mapping[str, Any]) -> None:
    from homeassistant.exceptions import HomeAssistantError

    if not export_enabled(options):
        raise HomeAssistantError("HA-MCP LLM API export is disabled")
