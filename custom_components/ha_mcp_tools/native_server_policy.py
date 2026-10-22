"""Read native catalogue settings from HA's data directory, without global env writes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path.name}")
    return value


def _flag(config: dict[str, Any], name: str) -> bool:
    value = os.environ.get(name.upper(), config.get(name, False))
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean setting: {name}")


@dataclass(frozen=True)
class NativePolicy:
    tool_states: dict[str, str] = field(default_factory=dict)
    llm_overrides: dict[str, bool] = field(default_factory=dict)
    pinned: frozenset[str] = frozenset()
    read_only: bool = False

    def enabled(self, name: str, *, writes: bool = False) -> bool:
        return self.tool_states.get(name) != "disabled" and not (
            writes and self.read_only
        )


def load_policy(directory: Path) -> NativePolicy:
    """File errors fail closed rather than re-enabling a user's hidden tools."""
    from ha_mcp.renamed_tools import current_tool_name
    from ha_mcp.transforms import DEFAULT_PINNED_TOOLS

    tools_config = _read_object(directory / "tool_config.json")
    flags = _read_object(directory / "feature_flags.json")
    if _flag(flags, "enable_tool_security_policies"):
        raise PermissionError(
            "Native runtime does not yet implement configured approval policies; retain the legacy runtime"
        )
    states = {
        current_tool_name(name): value
        for name, value in tools_config.get("tools", {}).items()
    }
    disabled = {
        name.strip()
        for name in os.environ.get("DISABLED_TOOLS", "").split(",")
        if name.strip()
    }
    pinned_env = {
        name.strip()
        for name in os.environ.get("PINNED_TOOLS", "").split(",")
        if name.strip()
    }
    for name in pinned_env:
        states[current_tool_name(name)] = "pinned"
    # A single spelling in both env vars is pinned; distinct aliases that
    # collapse onto one tool preserve the more restrictive disabled state.
    for name in disabled - pinned_env:
        states[current_tool_name(name)] = "disabled"
    pinned = (
        set(DEFAULT_PINNED_TOOLS) - {n for n, v in states.items() if v == "enabled"}
    ) | {n for n, v in states.items() if v == "pinned"}
    overrides = {
        name: value
        for name, value in tools_config.get("llm_api", {}).items()
        if isinstance(value, bool)
    }
    return NativePolicy(
        states, overrides, frozenset(pinned), _flag(flags, "read_only_mode")
    )


def catalogue_middleware(manager: Any) -> Any:
    """One live filter, with no growing stack of enable/disable transforms."""
    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp._vendor.fastmcp.server.middleware import Middleware
    from ha_mcp.llm_exposure import effective_llm_api_exposed

    class CataloguePolicy(Middleware):
        async def on_list_tools(self, context: Any, call_next: Any) -> Any:
            tools = await call_next(context)
            policy = manager.policy
            visible = []
            for tool in tools:
                writes = getattr(tool.annotations, "read_only_hint", False) is not True
                if not policy.enabled(tool.name, writes=writes):
                    continue
                meta = {
                    **(tool.meta or {}),
                    "ha_mcp": {
                        "llm_api_exposed": effective_llm_api_exposed(
                            tool.name, tool.tags, policy.llm_overrides
                        ),
                        "pinned": tool.name in policy.pinned,
                    },
                }
                visible.append(tool.model_copy(update={"meta": meta}))
            return visible

        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            if not manager.policy.enabled(context.message.name):
                raise ToolError("Unknown or disabled tool")
            return await call_next(context)

    return CataloguePolicy()
