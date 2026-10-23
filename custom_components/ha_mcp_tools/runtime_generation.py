"""Keep the library generation retained by native Core bindings process-wide."""

import importlib
import importlib.metadata
import sys

RESTART_REQUIRED = (
    "Native HA-MCP bindings retain the loaded library and SDK classes. "
    "Do not replace that generation in process; restart Home Assistant "
    "to load the installed package consistently."
)
SHARED_RESTART_REQUIRED = (
    "The HA-MCP component retains library interfaces from before a legacy "
    "package reload; restart Home Assistant before starting the native runtime."
)

# Survives entry unload: component classes still inherit from that SDK.
# 'unknown' reserves the generation before executor imports can start, so a
# cancelled startup cannot race a legacy purge or package replacement.
_NATIVE_VERSION: str | None = None
_COMPONENT_BINDINGS: tuple[object, object, object] | None = None


def capture_component_generation(
    tool_failure: object, owned_io: object, registry_lock: object
) -> None:
    """Remember the interfaces FileAccess retains, without reserving Native."""
    global _COMPONENT_BINDINGS
    if _COMPONENT_BINDINGS is None:
        _COMPONENT_BINDINGS = (tool_failure, owned_io, registry_lock)


def _check_component_generation() -> None:
    if _COMPONENT_BINDINGS is None:
        return
    current = (
        getattr(sys.modules.get("ha_mcp.tool_api"), "ToolFailure", None),
        getattr(sys.modules.get("ha_mcp.utils.owned_executor"), "await_owned_io", None),
        getattr(
            sys.modules.get("ha_mcp.utils.registry_update_lock"),
            "registry_update_lock",
            None,
        ),
    )
    if any(
        old is not new for old, new in zip(_COMPONENT_BINDINGS, current, strict=True)
    ):
        raise RuntimeError(SHARED_RESTART_REQUIRED)


def reserve_native_generation() -> None:
    global _NATIVE_VERSION
    # Check before reserving: a rejected first Native start must not disable
    # legacy hot upgrades, and no native domain/SDK imports have started yet.
    _check_component_generation()
    if _NATIVE_VERSION is None:
        _NATIVE_VERSION = "unknown"


def native_bound_version() -> str | None:
    return _NATIVE_VERSION


def _installed_version() -> str | None:
    from ha_mcp._version import _owns_ha_mcp

    owners = [name for name in ("ha-mcp", "ha-mcp-dev") if _owns_ha_mcp(name)]
    if len(owners) == 1:
        return importlib.metadata.version(owners[0])
    if owners:
        return None
    # Editable installs may have no top-level file manifest. Accept a single
    # installed distribution, without choosing between ambiguous providers.
    versions = []
    for name in ("ha-mcp", "ha-mcp-dev"):
        try:
            versions.append(importlib.metadata.version(name))
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions[0] if len(versions) == 1 else None


def bind_native_generation() -> None:
    """Called in the executor before importing native domain/SDK bindings."""
    global _NATIVE_VERSION
    reserve_native_generation()
    loaded = getattr(importlib.import_module("ha_mcp"), "__version__", None)
    installed = _installed_version()
    if (
        not isinstance(loaded, str)
        or installed is None
        or loaded != installed
        or _NATIVE_VERSION not in ("unknown", loaded)
    ):
        raise RuntimeError(RESTART_REQUIRED)
    _NATIVE_VERSION = loaded
