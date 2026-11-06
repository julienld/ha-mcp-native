"""Native tool-state rows from the unfiltered Engine catalogue."""

from types import SimpleNamespace
from typing import Any

from ha_mcp.llm_exposure import effective_llm_api_exposed
from ha_mcp.policy.model import bare_rule_gates
from ha_mcp.policy.persistence import load_policy as load_security_policy
from ha_mcp.settings_ui._tools_meta import (
    BPS_MANDATORY_TOOLS,
    FEATURE_GATED_TOOLS,
    effective_mandatory_tools,
    gate_is_beta,
)

from ..native_server_policy import env_tool_states, load_policy


def tool_rows(manager: Any) -> dict[str, Any]:
    policy = load_policy(manager.settings_ui.directory)
    approval = load_security_policy(manager.settings_ui.directory)
    settings: Any = SimpleNamespace(
        enable_mandatory_bps=policy.flag("enable_mandatory_bps"),
        enable_strict_mandatory_bps=policy.flag("enable_strict_mandatory_bps"),
    )
    mandatory = effective_mandatory_tools(settings)
    pinned = env_tool_states()
    rows = []
    for name, definition in manager.engine.tools.items():
        metadata = FEATURE_GATED_TOOLS.get(name)
        gate = metadata.get("disabled_by") if metadata else None
        available = not gate or (
            policy.flag(gate)
            and (not gate_is_beta(gate) or policy.flag("enable_beta_features"))
        )
        rows.append(
            {
                "name": name,
                "category": next(iter(sorted(definition.tags)), None),
                "state": policy.tool_states.get(
                    name, "pinned" if name in policy.pinned else "enabled"
                ),
                "available": available,
                "disabled_by": None if available else gate,
                "llm_api": effective_llm_api_exposed(
                    name, definition.tags, policy.llm_overrides
                ),
                "gated": bare_rule_gates(approval, name),
                "env_pinned": name in pinned,
                "mandatory": name in mandatory,
                "bps_locked": name in mandatory and name in BPS_MANDATORY_TOOLS,
            }
        )
    return {
        "success": True,
        "data": {
            "tools": rows,
            "count": len(rows),
            "policies_enabled": policy.flag("enable_tool_security_policies"),
            "policies_live": manager.approval_gate is not None
            and policy.flag("enable_tool_security_policies"),
            "llm_api_available": True,
        },
    }
