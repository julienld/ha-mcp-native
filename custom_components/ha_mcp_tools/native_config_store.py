"""Synchronous native configuration producers, called under one file lock."""

from pathlib import Path
from typing import Any

from ha_mcp.policy.decision_pin import is_pin_set
from ha_mcp.policy.model import (
    ALLOW_LIST_OMITTED_MESSAGE,
    Policy,
    drops_allow_list,
    gates_differ,
)
from ha_mcp.policy.persistence import load_policy as load_security_policy
from ha_mcp.policy.persistence import save_policy
from ha_mcp.settings_ui._persistence import _atomic_write_json

from .native_server_policy import _read_object, env_tool_states, load_policy


def prepare_tool_values(directory: Path, body: Any) -> dict[str, Any]:
    from ha_mcp.settings_ui._handlers_tools import (
        _coerce_llm_overrides,
        _coerce_tool_states,
    )

    if (
        not isinstance(body, dict)
        or not isinstance(body.get("states", {}), dict)
        or not isinstance(body.get("llm_api", {}), dict)
    ):
        raise ValueError("states and llm_api must be JSON objects")
    states = _coerce_tool_states(body.get("states", {}))
    policy = load_policy(directory)
    if (
        policy.flag("enable_mandatory_bps")
        and policy.flag("enable_strict_mandatory_bps")
        and states.get("ha_get_skill_guide") == "disabled"
        and policy.tool_states.get("ha_get_skill_guide") != "disabled"
    ):
        raise ValueError(
            "The skill guide is required while strict best-practices mode is enabled"
        )
    pinned = env_tool_states()
    if any(name in states and states[name] != value for name, value in pinned.items()):
        raise ValueError("Environment-pinned tools cannot be changed here")
    return {
        "tools": {name: value for name, value in states.items() if name not in pinned},
        "llm_api": _coerce_llm_overrides(body.get("llm_api", {})),
    }


def merge_config_locked(
    directory: Path, filename: str, updates: dict[str, Any]
) -> None:
    """Merge while the caller owns config_file_lock; never acquire it twice."""
    path = directory / filename
    _atomic_write_json(path, {**_read_object(path), **updates})


def prepare_policy_locked(directory: Path, body: Any) -> tuple[Policy, Policy]:
    new = Policy.model_validate(body)
    current = load_security_policy(directory)
    if drops_allow_list(new, current):
        raise ValueError(ALLOW_LIST_OMITTED_MESSAGE)
    if new.event_decisions_enabled and not is_pin_set(directory):
        raise ValueError("Set an approval PIN before enabling event decisions")
    return new, current


def save_policy_locked(directory: Path, body: Any) -> tuple[dict[str, Any], bool]:
    new, current = prepare_policy_locked(directory, body)
    if new.version != current.version:
        return {
            "error": "policy version mismatch",
            "current_version": current.version,
            "current_policy": current.model_dump(mode="json"),
        }, False
    save_policy(directory, new)
    return {"saved": True, "version": new.version + 1}, gates_differ(current, new)
