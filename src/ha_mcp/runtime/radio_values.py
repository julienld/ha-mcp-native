"""Transport-neutral radio action contracts and public response helpers."""

from dataclasses import dataclass, field
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


@dataclass(frozen=True)
class ActionSpec:
    """Metadata for one radio action.

    summary: one-line description (surfaced in unsupported-action errors).
    destructive: requires ``confirm=True`` before it will run.
    long_running: starts an operation that completes out-of-band (inclusion,
        rebuild routes, firmware) — the result documents how to follow up.
    required: parameter names that must be present in ``args`` (or supplied as
        the top-level ``device_id``).
    """

    summary: str
    destructive: bool = False
    long_running: bool = False
    required: tuple[str, ...] = field(default_factory=tuple)


def ok(radio: str, action: str, **data: Any) -> dict[str, Any]:
    """Build the standard success envelope for a radio action."""
    return {"success": True, "radio": radio, "action": action, **data}


def require(args: dict[str, Any], spec: ActionSpec, radio: str, action: str) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if any required arg is missing/empty."""
    missing = [k for k in spec.required if args.get(k) in (None, "")]
    if missing:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{radio}/{action} requires: {', '.join(missing)}",
                context={"radio": radio, "action": action, "missing": missing},
                suggestions=[f"Pass {m} (in params or as device_id)" for m in missing],
            )
        )


def integration_not_found(radio: str, domain: str) -> dict[str, Any]:
    """Standard degraded payload when an integration/config-entry is absent.

    For read-only actions (``network_status``) only — reporting the absent
    integration as a graceful ``success: True`` degradation is correct there.
    Write/management actions must call ``integration_required`` instead so an
    unconfigured integration surfaces as an error rather than a silent no-op.
    """
    return {
        "success": True,
        "radio": radio,
        "available": False,
        "warnings": [f"{domain} integration is not configured on this Home Assistant"],
    }


def integration_required(radio: str, domain: str) -> None:
    """Raise a ToolError when a write action's integration/config-entry is absent.

    The write-action counterpart to ``integration_not_found``: management actions
    must not report a no-op as ``success: True``, so this raises instead of
    degrading to an ``available: False`` payload.
    """
    raise_failure(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"{domain} integration is not configured on this Home Assistant; "
            f"cannot run this {radio} management action.",
            context={"radio": radio, "domain": domain, "available": False},
            suggestions=[f"Install/configure the {domain} integration first"],
        )
    )


def confirm_required(radio: str, action: str) -> None:
    """Raise the ToolError for a destructive action lacking confirm=True."""
    raise_failure(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{radio}/{action} is destructive; pass confirm=True to proceed",
            context={"radio": radio, "action": action, "destructive": True},
            suggestions=["Re-run with confirm=True once you intend the change"],
        )
    )
