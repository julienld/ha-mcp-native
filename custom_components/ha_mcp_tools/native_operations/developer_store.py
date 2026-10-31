"""Patch native tool settings without replacing unrelated UI configuration."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ha_mcp.policy.model import bare_rule_gates
from ha_mcp.policy.persistence import load_policy as load_security_policy
from ha_mcp.runtime.dev_values import _apply_gate_to_policy, _validate_state_change
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.config_write_lock import config_file_lock

from ..native_config_store import (
    merge_config_locked,
    prepare_policy_locked,
    prepare_tool_values,
    save_policy_locked,
)
from ..native_server_policy import _read_object, env_tool_states, load_policy


def require_dev_write(directory: Path, *, security: bool = False) -> Any:
    policy = load_policy(directory)
    if not policy.flag("enable_dev_mode"):
        raise ToolFailure(
            "FEATURE_DISABLED", "Developer mode is disabled", status="not_started"
        )
    if policy.read_only:
        raise ToolFailure(
            "READ_ONLY", "Native settings are read-only", status="not_started"
        )
    if security and not policy.flag("dev_tools_security_policy_access"):
        raise ToolFailure(
            "AUTH_INSUFFICIENT_PERMISSIONS",
            "Dev tools security policy access is disabled; enable it in the UI or environment",
            status="not_started",
        )
    return policy


def _prepare_gate(
    directory: Path, tool: str, gated: bool | None
) -> dict[str, Any] | None:
    if gated is None:
        return None
    current = load_security_policy(directory)
    updated, changed = _apply_gate_to_policy(current, tool, gated)
    if bare_rule_gates(updated, tool) != gated:
        raise ValueError(
            "The allow list's unconditional '*' rule approves every tool; remove it before gating this tool"
        )
    body = updated.model_dump(mode="json")
    prepare_policy_locked(directory, body)
    return body if changed else None


def patch_tool(
    directory: Path,
    tool: str,
    state: str | None,
    llm_api: bool | None,
    gated: bool | None,
) -> tuple[dict[str, Any], bool]:
    directory.mkdir(parents=True, exist_ok=True)
    with config_file_lock(directory):
        native = require_dev_write(directory, security=gated is not None)
        settings = SimpleNamespace(
            enable_mandatory_bps=native.flag("enable_mandatory_bps"),
            enable_strict_mandatory_bps=native.flag("enable_strict_mandatory_bps"),
        )
        config = _read_object(directory / "tool_config.json")
        states, llm = dict(config.get("tools", {})), dict(config.get("llm_api", {}))
        if state is not None and _validate_state_change(
            tool, state, settings, env_tool_states()
        ):
            states[tool] = state
        if llm_api is not None:
            if type(llm_api) is not bool:
                raise ValueError("llm_api must be a boolean")
            llm[tool] = llm_api
        if gated is not None and type(gated) is not bool:
            raise ValueError("gated must be a boolean")
        values = prepare_tool_values(directory, {"states": states, "llm_api": llm})
        policy_body = _prepare_gate(directory, tool, gated)
        return _commit_tool(directory, tool, state, llm_api, gated, values, policy_body)


def _commit_tool(
    directory: Path,
    tool: str,
    state: str | None,
    llm_api: bool | None,
    gated: bool | None,
    values: dict[str, Any],
    policy_body: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    data: dict[str, Any] = {"tool": tool, "restart_required": state is not None}
    confirmed: list[str] = []
    try:
        if state is not None or llm_api is not None:
            merge_config_locked(directory, "tool_config.json", values)
            confirmed.append("tool_config.json")
            if state is not None:
                data["state"] = state
            if llm_api is not None:
                data["llm_api"] = llm_api
        changed = False
        if policy_body is not None:
            result, changed = save_policy_locked(directory, policy_body)
            if "current_version" in result:
                raise ValueError(
                    "Policy changed concurrently; read the tool and policy state"
                )
            confirmed.append("tool_policy.json")
        if gated is not None:
            data.update(gated=gated, policy_rules_changed=changed)
        return {"success": True, "data": data}, changed
    except (OSError, ValueError) as error:
        raise ToolFailure(
            "OUTCOME_UNKNOWN",
            "Settings persistence did not finish; read list_tools and get_policy before retrying",
            data={
                "success": False,
                "error": {
                    "code": "OUTCOME_UNKNOWN",
                    "message": "Read list_tools and get_policy before retrying",
                },
                "partial_commit": bool(confirmed),
                "confirmed_files": confirmed,
                "persisted": data,
            },
            status="outcome_unknown",
        ) from error
