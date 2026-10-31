"""Shared read/write path for the tool-security-policy document.

Two MCP tools edit the same ``tool_policy.json``: the developer-mode
``ha_dev_manage_settings`` (``get_policy`` / ``set_policy``) and the
standalone ``ha_manage_security_policy`` (issue #2148). Both drive it
through these helpers so validation, the lock choreography, the
optimistic-concurrency check, the remember-cache invalidation, and the
response shapes cannot drift between the two surfaces.

Only the strings that name the calling tool's own actions vary, and
those come from the :class:`PolicyCaller` each tool passes in.
"""

import logging
from typing import Any

from ..backends.legacy import legacy_errors_sync
from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .document_values import PolicyCaller as PolicyCaller
from .document_values import policy_write_warnings, prepare_policy_document

_prepare_policy_document = legacy_errors_sync(prepare_policy_document)

logger = logging.getLogger(__name__)


def _clear_remember_cache(server: Any | None) -> None:
    """Clear the approval remember-cache after a rule change.

    Called only when the rules or ``rule_effect`` actually changed, so a
    missing queue is worth a WARNING: the gates the operator just wrote are
    not what the engine (wherever it runs) is still remembering.
    """
    queue = getattr(server, "approval_queue", None)
    if queue is None:
        logger.warning(
            "Policy rules changed but no approval queue is live in this "
            "process; remembered approvals were not cleared."
        )
        return
    queue.clear_remember_cache()


async def get_policy(server: Any) -> dict[str, Any]:
    """Return the full tool-security policy plus its enforcement status."""
    import asyncio

    from ..config import get_global_settings
    from ..utils.data_paths import get_data_dir
    from .persistence import load_policy

    try:
        policy = await asyncio.to_thread(load_policy, get_data_dir())
    except ValueError as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_INVALID,
                f"tool_policy.json is invalid: {exc}",
                suggestions=["Inspect or delete the file, then retry"],
            )
        )
    return {
        "success": True,
        "data": {
            "policy": policy.model_dump(mode="json"),
            "policies_enabled": (get_global_settings().enable_tool_security_policies),
            "policies_live": getattr(server, "approval_queue", None) is not None,
        },
    }


async def set_policy(
    policy: dict[str, Any] | None,
    expected_version: int | None,
    caller: PolicyCaller,
    server: Any,
) -> dict[str, Any]:
    """Write the full tool-security policy (validated, version-guarded)."""
    import asyncio

    from ..utils.config_write_lock import get_config_write_lock, run_with_file_lock

    new_policy, expected = _prepare_policy_document(policy, expected_version, caller)
    # Serialize the load-check-save against the web PUT handler + set_tool
    # (asyncio lock) and other processes (file lock, in the thread).
    async with get_config_write_lock():
        return await asyncio.to_thread(
            run_with_file_lock, _commit_policy, new_policy, expected, caller, server
        )


def _pre_save_warnings(
    new_policy: Any, current: Any, expected: int | None, server: Any
) -> list[str]:
    from ..config import get_global_settings

    return policy_write_warnings(
        new_policy,
        current,
        expected,
        policies_enabled=get_global_settings().enable_tool_security_policies,
        policies_live=getattr(server, "approval_queue", None) is not None,
    )


def _commit_policy(
    new_policy: Any,
    expected: int | None,
    caller: PolicyCaller,
    server: Any,
) -> dict[str, Any]:
    """Load current policy, version-check against ``expected``, save, report.

    MUST run while holding ``get_config_write_lock()`` (called via
    ``asyncio.to_thread`` from :func:`set_policy`).
    """
    from ..utils.data_paths import get_data_dir
    from .decision_pin import is_pin_set
    from .model import ALLOW_LIST_OMITTED_MESSAGE, drops_allow_list, gates_differ
    from .persistence import load_policy, save_policy

    data_dir = get_data_dir()
    try:
        current = load_policy(data_dir)
    except ValueError as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_INVALID,
                f"existing tool_policy.json is invalid: {exc}",
                suggestions=["Inspect or delete the file, then retry"],
            )
        )
    if drops_allow_list(new_policy, current):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                ALLOW_LIST_OMITTED_MESSAGE,
                suggestions=[f"Call {caller.get_call} and resend the edited document"],
            )
        )
    if new_policy.event_decisions_enabled and not is_pin_set(data_dir):
        # No MCP tool takes the PIN as a parameter, returns it, or writes
        # the file it lives in: the component's deny floor blocks that
        # basename on read, write and delete and keeps it out of a
        # directory listing, whatever extra file paths an operator
        # configures. It is the one thing in this feature
        # meant to come from the person rather than from the agent — which
        # is a statement about this server's surface, not a guarantee about
        # every path to the disk an operator may open by other means.
        # Enabling the toggle without a PIN would produce a policy the
        # listener refuses to act on anyway.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "event_decisions_enabled is true but no approval PIN is "
                "stored. The PIN can only be set by a person, on the Tool "
                "Security Policies tab of the settings UI.",
            )
        )
    if expected is not None and expected != current.version:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "policy version mismatch — reload with "
                f"{caller.get_call} before saving",
                context={
                    "current_version": current.version,
                    "current_policy": current.model_dump(mode="json"),
                },
            )
        )
    rules_changed = current.rules != new_policy.rules
    warnings = _pre_save_warnings(new_policy, current, expected, server)
    # Rebase onto the on-disk version so save_policy bumps to current+1.
    save_policy(data_dir, new_policy.model_copy(update={"version": current.version}))
    if gates_differ(current, new_policy):
        # The write has landed. Nothing after this point may turn a committed
        # save into a failure the caller would retry.
        try:
            _clear_remember_cache(server)
        except Exception:
            logger.warning(
                "policy saved but remember-cache clear failed", exc_info=True
            )
            warnings.append(
                "The policy was saved, but remembered approvals could not be "
                "cleared; an earlier approval may still be honored until its "
                "TTL expires."
            )
    result: dict[str, Any] = {
        "success": True,
        "data": {"version": current.version + 1, "rules_changed": rules_changed},
    }
    if warnings:
        result["warnings"] = warnings
    return result
