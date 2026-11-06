"""Legacy settings and middleware around shared secret redaction."""

from __future__ import annotations

from typing import Any

from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from .config import get_global_settings
from .runtime import redaction as _shared_redaction
from .runtime.redaction import (
    REDACTED_EMPTY,
    REDACTED_KNOWN,
    REDACTED_SET,
    _clear_known_secret_values,
    is_password_flow_field,
    is_sentinel,
    known_secret_values,
    redact_flow_schema,
    redact_options_by_flow_schema,
    register_known_secret_values,
    scrub_obj,
    scrub_text,
    sentinel_for,
    sentinel_option_keys,
    with_serialized_variants,
)


def redaction_enabled() -> bool:
    """Live per-request read of the ``redact_secrets`` flag."""
    return bool(get_global_settings().redact_secrets)


def app_option_keys() -> str:
    """Read the operator list through the legacy settings owner."""
    return str(getattr(get_global_settings(), "redact_app_option_keys", ""))


def redact_addon_info(
    addon: dict[str, Any], slug: str
) -> tuple[dict[str, Any], list[str]]:
    return _shared_redaction.redact_addon_info(
        addon, slug, option_keys=app_option_keys()
    )


def redact_addon_options(options: Any, schema: Any, slug: str = "") -> Any:
    return _shared_redaction.redact_addon_options(
        options, schema, slug, option_keys=app_option_keys()
    )


def collect_addon_secret_values(options: Any, schema: Any, slug: str = "") -> set[str]:
    return _shared_redaction.collect_addon_secret_values(
        options, schema, slug, option_keys=app_option_keys()
    )


class RedactSecretsMiddleware(Middleware):
    """Scrub known secret values from every tool response and tool error.

    Registered immediately BEFORE the visibility outbound scan, which must
    stay innermost so it sees truly raw output (a secret value could embed a
    hidden entity id); since that middleware only refuses or passes through
    — never rewrites — this scrubber still sees the unmodified tool output.
    Consults the live flag per call and is a passthrough while it is off or
    no secret values are known.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            result = await call_next(context)
        except Exception as exc:
            # Not just ToolError: FastMCP forwards non-ToolError exception
            # text to clients too (mask_error_details is not enabled), so a
            # ValueError carrying a credential would leak through. Mutating
            # args preserves the exception type and traceback.
            if redaction_enabled():
                secrets = with_serialized_variants(known_secret_values())
                if secrets and getattr(exc, "args", None):
                    # scrub_obj covers non-string args too (dicts/lists a
                    # library packed into the exception); non-JSON-shaped
                    # args pass through unchanged.
                    exc.args = tuple(scrub_obj(arg, secrets) for arg in exc.args)
            raise
        if not redaction_enabled():
            return result
        secrets = with_serialized_variants(known_secret_values())
        if not secrets:
            return result
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                scrubbed = scrub_text(text, secrets)
                if scrubbed != text:
                    block.text = scrubbed
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            result.structured_content = scrub_obj(structured, secrets)
        return result


__all__ = [
    "REDACTED_EMPTY",
    "REDACTED_KNOWN",
    "REDACTED_SET",
    "RedactSecretsMiddleware",
    "_clear_known_secret_values",
    "collect_addon_secret_values",
    "is_password_flow_field",
    "is_sentinel",
    "redact_addon_options",
    "redact_flow_schema",
    "redact_options_by_flow_schema",
    "redaction_enabled",
    "register_known_secret_values",
    "sentinel_for",
    "sentinel_option_keys",
]
