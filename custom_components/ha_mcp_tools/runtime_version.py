"""Version admission before importing a runtime's Core owners."""

from collections.abc import Mapping
from typing import Any

from packaging.version import InvalidVersion, Version

from .const import (
    MIN_NATIVE_HOME_ASSISTANT_VERSION,
    OPT_RUNTIME_MODE,
    RUNTIME_LEGACY,
    RUNTIME_NATIVE,
)


def version_supported(installed: str, minimum: str) -> bool:
    """Use the config flow's version comparison and fail closed on unknown input."""
    try:
        return Version(installed) >= Version(minimum)
    except InvalidVersion:
        return False


def require_native_version(installed: str) -> None:
    """Protect stored/imported native entries and direct manager startup too."""
    if not version_supported(installed, MIN_NATIVE_HOME_ASSISTANT_VERSION):
        raise ValueError(
            f"Native HA-MCP requires Home Assistant {MIN_NATIVE_HOME_ASSISTANT_VERSION} "
            f"or newer; this instance runs {installed}. Existing legacy entries remain available."
        )


def runtime_version_errors(
    values: Mapping[str, Any], existing: Mapping[str, Any], installed: str
) -> dict[str, str]:
    mode = values.get(OPT_RUNTIME_MODE, existing.get(OPT_RUNTIME_MODE, RUNTIME_LEGACY))
    if mode == RUNTIME_NATIVE and not version_supported(
        installed, MIN_NATIVE_HOME_ASSISTANT_VERSION
    ):
        return {OPT_RUNTIME_MODE: "unsupported_home_assistant"}
    return {}
