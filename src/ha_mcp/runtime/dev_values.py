"""Developer-setting validation shared without a global settings instance."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

_COERCE_MISS = object()

HUMAN_BACKUP_CONTROLS = frozenset(
    {
        "enable_snapshot_actions",
        "backup_read_only",
        "enable_snapshot_delete",
        "snapshot_delete_min_age_days",
    }
)


_HUMAN_ONLY_SETTINGS: dict[str, tuple[str, str]] = {
    "enable_security_policy_tool": (
        "it registers ha_manage_security_policy, which can rewrite the tool "
        "security policies gating the agent.",
        "Change it in the web settings UI (Tool Security Policies tab) or via "
        "the ENABLE_SECURITY_POLICY_TOOL env var.",
    ),
    "dev_tools_security_policy_access": (
        "it is the toggle governing their own access to tool security policies.",
        "Change it in the web settings UI (Server Settings tab, Developer "
        "section) or via the HAMCP_DEV_SECURITY_POLICY_ACCESS env var.",
    ),
    "custom_instructions": (
        "its text is sent to every connected agent as server instructions.",
        "Change it in the web settings UI (Server Settings tab, Advanced) or "
        "via the HAMCP_CUSTOM_INSTRUCTIONS env var.",
    ),
}


def _coerce_setting_value(fname: str, raw: Any, ftype: type) -> Any:
    """Coerce/validate ``raw`` against the registry field type.

    Accepts the natural JSON type plus common MCP-client
    stringifications ("true"/"false" for bools, numeric strings
    for int/float). Raises ``ToolError`` on mismatch.
    """
    if ftype is bool:
        coerced = _coerce_bool_setting(raw)
    elif ftype is int:
        coerced = _coerce_int_setting(raw)
    elif ftype is float:
        coerced = _coerce_float_setting(raw)
    elif ftype is str:
        coerced = _coerce_str_setting(fname, raw)
    else:
        coerced = _COERCE_MISS
    if coerced is not _COERCE_MISS:
        return coerced
    raise_failure(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{fname!r} must be of type {ftype.__name__}, got {type(raw).__name__}",
            context={"setting": fname, "value": raw},
        )
    )
    return None


def _coerce_bool_setting(raw: Any) -> Any:
    """Coerce ``raw`` to bool, or ``_COERCE_MISS`` if not coercible."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    return _COERCE_MISS


def _coerce_int_setting(raw: Any) -> Any:
    """Coerce ``raw`` to int, or ``_COERCE_MISS`` if not coercible."""
    if isinstance(raw, bool):
        pass  # bool is an int subclass; reject below
    elif isinstance(raw, int):
        return raw
    elif isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            pass
    return _COERCE_MISS


def _coerce_float_setting(raw: Any) -> Any:
    """Coerce ``raw`` to float, or ``_COERCE_MISS`` if not coercible."""
    if isinstance(raw, bool):
        pass
    elif isinstance(raw, int | float):
        return float(raw)
    elif isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            pass
    return _COERCE_MISS


def _coerce_str_setting(fname: str, raw: Any) -> Any:
    """Coerce ``raw`` to str, or ``_COERCE_MISS`` if not a string.

    Raises ``ToolError`` when a string contains a null byte.
    """
    if isinstance(raw, str):
        if "\x00" in raw:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{fname!r} value contains a null byte",
                )
            )
        return raw
    return _COERCE_MISS


def _validate_state_change(
    tool: str, state: str, settings: Any, env_pinned: dict[str, str]
) -> bool:
    """Validate a state change; return whether it should be persisted.

    Rejects invalid states, env-pinned flips, and disabling a mandatory
    tool (base MANDATORY_TOOLS always, plus the BPS-locked set while
    strict best-practices mode is on) — the web Tools tab locks those
    rows, so the headless path must refuse rather than persist a disable
    that apply_tool_visibility silently reverts at startup. Env-pinned
    no-op re-sends return False (nothing to persist); anything else that
    passes returns True.
    """
    from ha_mcp.settings_ui._tools_meta import (
        _VALID_STATES,
        BPS_MANDATORY_TOOLS,
        effective_mandatory_tools,
    )

    if state not in _VALID_STATES:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"state must be one of {sorted(_VALID_STATES)}",
            )
        )
    if tool in env_pinned:
        if env_pinned[tool] != state:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{tool!r} is pinned by DISABLED_TOOLS / PINNED_TOOLS "
                    f"to {env_pinned[tool]!r}; unset the env var to change it.",
                )
            )
        return False
    if state == "disabled" and tool in effective_mandatory_tools(settings):
        if tool in BPS_MANDATORY_TOOLS:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Refusing to disable {tool!r} while strict "
                    "best-practices mode (enable_strict_mandatory_bps) is on.",
                    suggestions=[
                        "Turn off strict best-practices mode first, then retry."
                    ],
                )
            )
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{tool!r} is a mandatory tool and is always kept enabled; "
                "it cannot be disabled.",
            )
        )
    return True


def _apply_gate_to_policy(policy: Any, tool: str, gated: bool) -> tuple[Any, bool]:
    """Return (policy, changed) after adding/removing the bare gate rule.

    Pure (no I/O): manages only the bare unconditional rule (when == [])
    for ``tool``; predicate-bearing rules are preserved. Under an allow
    list the bare rule approves the tool, so gating removes it.
    """
    from ha_mcp.policy.model import Rule

    want_bare = gated != (policy.rule_effect == "allow")
    has_bare = any(r.tool_name == tool and not r.when for r in policy.rules)
    if want_bare and not has_bare:
        # Insert before the first wildcard rule (mirrors the web UI's
        # wildcardInsertIndex): in a require-approval list
        # find_matching_rule() is first-match, so a gate appended after a
        # `*` rule would never supply this tool's remember_minutes /
        # matched_rule. An allow list does not depend on the order.
        rules = list(policy.rules)
        insert_at = next(
            (i for i, r in enumerate(rules) if r.tool_name == "*"), len(rules)
        )
        rules.insert(insert_at, Rule(tool_name=tool))
        updated = policy.model_copy(update={"rules": rules})
        return updated, True
    if not want_bare and has_bare:
        updated = policy.model_copy(
            update={"rules": [r for r in policy.rules if r.tool_name != tool or r.when]}
        )
        return updated, True
    return policy, False


__all__ = [
    "_HUMAN_ONLY_SETTINGS",
]
