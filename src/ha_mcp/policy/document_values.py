"""Policy document validation and warnings shared by native and legacy owners."""

from typing import Any, NamedTuple

from pydantic import ValidationError

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure

from .model import Policy


class PolicyCaller(NamedTuple):
    """How the calling tool spells itself in user-facing messages."""

    tool: str
    get_action: str
    set_action: str

    @property
    def get_call(self) -> str:
        """The example call that reads the current policy, e.g. ``ha_x('get')``."""
        return f"{self.tool}('{self.get_action}')"


def prepare_policy_document(
    policy: dict[str, Any] | None, expected_version: int | None, caller: PolicyCaller
) -> tuple[Policy, int | None]:
    if not isinstance(policy, dict):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "'policy' (a policy object) is required for "
                f"action='{caller.set_action}'",
                suggestions=[f"Call {caller.get_call} for the shape"],
            )
        )
    # ``rules`` defaults to [] on the model, so an omitted key would validate
    # and silently delete every approval gate. Demand it explicitly instead.
    if "rules" not in policy:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "'rules' is missing from the policy document. "
                f"action='{caller.set_action}' replaces the WHOLE document, so "
                "an omitted 'rules' would delete every approval gate. Send "
                '"rules": [] to clear them deliberately.',
                suggestions=[f"Call {caller.get_call} and resend the edited document"],
            )
        )
    try:
        new_policy = Policy.model_validate(policy)
    except (ValidationError, ValueError) as exc:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"policy failed schema validation: {exc}",
            )
        )
    # Compare against the COERCED model version so a JSON string "3" matches
    # the on-disk int 3; ``"version" in policy`` distinguishes an omitted
    # version (no concurrency check) from an explicit one.
    expected = (
        expected_version
        if expected_version is not None
        else (new_policy.version if "version" in policy else None)
    )
    return new_policy, expected


def policy_write_warnings(
    new_policy: Any,
    current: Any,
    expected: int | None,
    *,
    policies_enabled: bool,
    policies_live: bool,
) -> list[str]:
    """Every warning the response carries that does not depend on the write.

    Computed BEFORE ``save_policy`` runs: these read the Settings singleton
    and the server object, and an exception from either after a committed
    write would surface as INTERNAL_ERROR on a save that actually landed —
    the caller would then retry and hit a spurious version mismatch.
    """
    warnings: list[str] = []
    if expected is None:
        warnings.append(
            "No version supplied; wrote without an optimistic-concurrency check."
        )
    removed = [rule for rule in current.rules if rule not in new_policy.rules]
    if removed:
        names = ", ".join(sorted({rule.tool_name for rule in removed}))
        warnings.append(
            f"This write removed {len(removed)} existing rule(s), covering: "
            f"{names}. Resend them if that was not intended."
        )
    if current.event_decisions_enabled and not new_policy.event_decisions_enabled:
        # Whole-document replacement makes an omitted field indistinguishable
        # from a deliberate false, and a document copied from an example that
        # predates the field carries neither. Closing a channel the user opened
        # is not something the caller should have to diff the response to spot.
        warnings.append(
            "This write switched off deciding approvals from Home Assistant "
            "events (event_decisions_enabled). Omitting the field has the "
            "same effect as sending false, so resend the document with "
            '"event_decisions_enabled": true if that was not intended. The '
            "stored PIN is untouched either way."
        )
    if new_policy.rule_effect == "allow":
        # In an allow list an added rule is the loosening direction.
        added = [rule for rule in new_policy.rules if rule not in current.rules]
        if added:
            names = ", ".join(sorted({rule.tool_name for rule in added}))
            warnings.append(
                f"This write added {len(added)} approving rule(s), covering: "
                f"{names}. Calls they match now run without approval."
            )
    if current.rule_effect != new_policy.rule_effect:
        warnings.append(
            f"This write switched rule_effect from '{current.rule_effect}' to "
            f"'{new_policy.rule_effect}': every rule now "
            + (
                "approves the calls it matches, and every other call requires approval."
                if new_policy.rule_effect == "allow"
                else "requires approval for the calls it matches, and every "
                "other call runs without approval."
            )
        )
    # An empty require-approval list gates nothing, so its enforcement state
    # is moot; an empty allow list gates every call and still needs it.
    if not new_policy.rules and new_policy.rule_effect == "require_approval":
        return warnings
    if not policies_enabled:
        # Same "won't enforce" signal set_tool(gated=True) gives, so authoring
        # rules while the engine is off doesn't look like a live gate.
        warnings.append(
            "Tool security policies are disabled "
            "(enable_tool_security_policies=false); these rules are stored "
            "but won't enforce until policies are enabled and the server "
            "restarts."
        )
    elif not policies_live:
        # Flag on but no queue: the enabled-but-not-yet-live window (a
        # restart is pending) or the middleware failed to load. Either way
        # nothing is gated right now, which plain success would hide.
        warnings.append(
            "The approval engine is NOT running in this process (a restart is "
            "pending, or the policy middleware failed to load — check the "
            "server log); these rules are stored but nothing is gated until "
            "it is live."
        )
    return warnings
