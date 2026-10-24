"""Keep caller options outside the tool-owned query envelope."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def merge_core_options(
    params: dict[str, Any], options: dict[str, Any] | None
) -> dict[str, Any]:
    """Pass native options through while retaining the tool's bounded query envelope."""
    if not options:
        return params
    reserved = set(params) | {"id", "type"}
    if overlap := reserved.intersection(options):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Use the tool parameters for protected query fields",
                context={"protected_fields": sorted(overlap)},
            )
        )
    return {**params, **options}
