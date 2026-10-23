"""Schema-driven secret redaction behind the ``redact_secrets`` flag (#2157).

When ``Settings.redact_secrets`` is on (``redact_secrets`` addon option,
``REDACT_SECRETS`` env var, or the Server Settings web-UI toggle), three
surfaces stop returning operator secrets verbatim:

1. **Add-on options** (``ha_get_app`` detail form, ``ha_manage_app``'s
   proxy-mode 403 hints): every ``options`` value whose Supervisor-serialized
   ``schema`` entry carries ``format: "password"`` is replaced with a
   set/empty sentinel. The schema is authoritative — keys without a schema
   entry are never touched, and no name-based guessing happens.
2. **Integration options** (``ha_get_integration``): fields the
   options-flow schema marks with a password text selector are replaced the
   same way, on both the harvested options and the ``include_schema=True``
   schema echo (whose ``suggested_value`` carries the live value).
3. **Every other tool response**: password values seen while serving 1 and 2
   are remembered in a process-local known-secrets set, and
   :class:`RedactSecretsMiddleware` (registered just outside the
   entity-visibility outbound scan, which stays innermost) scrubs any
   occurrence in any tool response or tool error to ``<redacted>``.

The set/empty sentinel split keeps the genuinely useful diagnostic — "is
this credential configured at all?" — answerable without disclosing the
value. ``ha_manage_app`` and the config-entry flow writes reject
submitted values that are (or contain) a sentinel — unconditionally, not
only while the flag is on — so a redacted marker can never be round-tripped
into a live config (add-on writes still merge from the real, unredacted
current options server-side).

With the flag off (the default) every redaction path here is a passthrough
and tool output is byte-identical to the legacy behavior. The sentinel write
guards are the deliberate exception and stay active either way.
"""

from __future__ import annotations

from typing import Any

from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from .config import get_global_settings
from .runtime.redaction import (
    REDACTED_EMPTY,
    REDACTED_KNOWN,
    REDACTED_SET,
    _clear_known_secret_values,
    collect_addon_secret_values,
    is_password_flow_field,
    is_sentinel,
    known_secret_values,
    redact_addon_options,
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
