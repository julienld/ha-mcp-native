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


def _flag(config: dict[str, Any], name: str, default: bool = False) -> bool:
    value = os.environ.get(name.upper(), config.get(name, default))
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
    security_policy: Any = None
    flags: dict[str, Any] = field(default_factory=dict)
    enabled_modules: frozenset[str] | None = None
    custom_instructions: str = ""

    def flag(self, name: str, default: bool | None = None) -> bool:
        if default is None:
            from ha_mcp.config_settings import Settings

            declared = Settings.model_fields[name].default
            default = declared if isinstance(declared, bool) else False
        return _flag(self.flags, name, default)

    def enabled(self, name: str, *, writes: bool = False, source_module: str | None = None) -> bool:
        from ha_mcp.settings_ui._tools_meta import MANDATORY_TOOLS

        module_enabled = self.enabled_modules is None or source_module in (None, "server") or source_module in self.enabled_modules
        if name == "ha_get_skill_guide" and self.flag("enable_mandatory_bps") and self.flag("enable_strict_mandatory_bps"):
            return True
        return module_enabled and (name in MANDATORY_TOOLS or self.tool_states.get(name) != "disabled") and not (writes and self.read_only)

    def tool_enabled(self, definition: Any) -> bool:
        from ha_mcp.settings_ui._tools_meta import FEATURE_GATED_TOOLS, gate_is_beta

        gate = FEATURE_GATED_TOOLS.get(definition.name, {}).get("disabled_by")
        if gate and (not self.flag(gate) or (gate_is_beta(gate) and not self.flag("enable_beta_features"))):
            return False
        return self.enabled(definition.name, source_module=definition.source_module)


def load_policy(directory: Path) -> NativePolicy:
    """File errors fail closed rather than re-enabling a user's hidden tools."""
    from ha_mcp.renamed_tools import current_tool_name
    from ha_mcp.runtime.module_selection import parse_enabled_modules
    from ha_mcp.transforms import DEFAULT_PINNED_TOOLS

    tools_config = _read_object(directory / "tool_config.json")
    flags = {**_read_object(directory / "backup_settings.json"), **_read_object(directory / "feature_flags.json")}
    module_setting = os.environ.get("ENABLED_TOOL_MODULES", flags.get("enabled_tool_modules", "all"))
    enabled_modules = parse_enabled_modules(str(module_setting))
    instructions = os.environ.get("HAMCP_CUSTOM_INSTRUCTIONS", flags.get("custom_instructions", ""))
    security_policy = None
    if _flag(flags, "enable_tool_security_policies"):
        from ha_mcp.policy.persistence import load_policy as load_approval_policy
        from ha_mcp.policy.persistence import migrate_policy_any_semantics

        migrate_policy_any_semantics(directory)
        security_policy = load_approval_policy(directory)
    states = {
        current_tool_name(name): value
        for name, value in tools_config.get("tools", {}).items()
    }
    states.update(env_tool_states())
    pinned = (
        set(DEFAULT_PINNED_TOOLS) - {n for n, v in states.items() if v == "enabled"}
    ) | {n for n, v in states.items() if v == "pinned"}
    overrides = {
        name: value
        for name, value in tools_config.get("llm_api", {}).items()
        if isinstance(value, bool)
    }
    return NativePolicy(
        states,
        overrides,
        frozenset(pinned),
        _flag(flags, "read_only_mode"),
        security_policy,
        flags,
        frozenset(enabled_modules) if enabled_modules is not None else None,
        str(instructions),
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
                definition = manager.engine.tools.get(tool.name)
                source_module = definition.source_module if definition is not None else None
                if not policy.enabled(tool.name, writes=writes, source_module=source_module) or (definition is not None and not policy.tool_enabled(definition)):
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
            definition = manager.engine.tools.get(context.message.name)
            source_module = definition.source_module if definition is not None else None
            if not manager.policy.enabled(context.message.name, source_module=source_module) or (definition is not None and not manager.policy.tool_enabled(definition)):
                raise ToolError("Unknown or disabled tool")
            return await call_next(context)

    return CataloguePolicy()


def env_tool_states() -> dict[str, str]:
    """Environment pins take precedence, with restrictive alias collisions."""
    from ha_mcp.renamed_tools import current_tool_name

    disabled = {
        name.strip()
        for name in os.environ.get("DISABLED_TOOLS", "").split(",")
        if name.strip()
    }
    pinned = {
        name.strip()
        for name in os.environ.get("PINNED_TOOLS", "").split(",")
        if name.strip()
    }
    states = {current_tool_name(name): "pinned" for name in pinned}
    states.update({current_tool_name(name): "disabled" for name in disabled - pinned})
    return states
