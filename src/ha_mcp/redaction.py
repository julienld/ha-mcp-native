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
    _MIN_KNOWN_SECRET_LEN as _MIN_KNOWN_SECRET_LEN,
)
from .runtime.redaction import (
    _PEM_PRIVATE_KEY_MARKER as _PEM_PRIVATE_KEY_MARKER,
)
from .runtime.redaction import (
    _SENTINELS as _SENTINELS,
)
from .runtime.redaction import (
    REDACTED_EMPTY as REDACTED_EMPTY,
)
from .runtime.redaction import (
    REDACTED_KNOWN as REDACTED_KNOWN,
)
from .runtime.redaction import (
    REDACTED_SET as REDACTED_SET,
)
from .runtime.redaction import (
    _clear_known_secret_values as _clear_known_secret_values,
)
from .runtime.redaction import (
    _collect_sentinel_paths as _collect_sentinel_paths,
)
from .runtime.redaction import (
    _iter_schema_entries as _iter_schema_entries,
)
from .runtime.redaction import (
    _known_secrets as _known_secrets,
)
from .runtime.redaction import (
    _known_secrets_lock as _known_secrets_lock,
)
from .runtime.redaction import (
    _redact_flow_schema_in_place as _redact_flow_schema_in_place,
)
from .runtime.redaction import (
    _redact_nested_sections as _redact_nested_sections,
)
from .runtime.redaction import (
    carries_sentinel as carries_sentinel,
)
from .runtime.redaction import (
    collect_addon_secret_values as collect_addon_secret_values,
)
from .runtime.redaction import (
    contains_pem_private_key as contains_pem_private_key,
)
from .runtime.redaction import (
    harvest_secret_strings as harvest_secret_strings,
)
from .runtime.redaction import (
    is_password_flow_field as is_password_flow_field,
)
from .runtime.redaction import (
    is_sentinel as is_sentinel,
)
from .runtime.redaction import (
    known_secret_values as known_secret_values,
)
from .runtime.redaction import (
    known_values_for_option_item as known_values_for_option_item,
)
from .runtime.redaction import (
    logger as logger,
)
from .runtime.redaction import (
    redact_addon_options as redact_addon_options,
)
from .runtime.redaction import (
    redact_flow_schema as redact_flow_schema,
)
from .runtime.redaction import (
    redact_options_by_flow_schema as redact_options_by_flow_schema,
)
from .runtime.redaction import (
    register_known_secret_values as register_known_secret_values,
)
from .runtime.redaction import (
    scrub_obj as scrub_obj,
)
from .runtime.redaction import (
    scrub_text as scrub_text,
)
from .runtime.redaction import (
    sentinel_for as sentinel_for,
)
from .runtime.redaction import (
    sentinel_option_keys as sentinel_option_keys,
)
from .runtime.redaction import (
    sentinel_replacement as sentinel_replacement,
)
from .runtime.redaction import (
    with_serialized_variants as with_serialized_variants,
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
