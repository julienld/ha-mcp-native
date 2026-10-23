"""Step submission and flow walking for config-entry flows.

Extracted from ``config_entry_flow.py`` (which holds the public create/update
entry points) when that module crossed the ~1000-line split threshold. Holds
the two walkers that drive a flow to completion, the step submission that
translates HA's 4xx bodies into structured errors. Flow introspection lives in
``config_entry_flow_introspect``. Imports the menu and form step
handling from ``config_entry_flow_menu`` / ``config_entry_flow_form``; the
public entry points in ``config_entry_flow`` import from here.
"""

import asyncio
import logging
from enum import StrEnum
from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, HomeAssistantAPIError, create_error_response
from ha_mcp.runtime.redaction import redact_flow_schema

from ..errors import raise_failure as raise_tool_error
from .introspect import fetch_helper_flow_info
from .settings import no_answer_errors, redaction_enabled

logger = logging.getLogger(__name__)

# Flow step types an MCP client cannot drive: external steps need a browser
# (OAuth / cloud authorization), progress steps wait on HA-side async work.
# Surfaced as structured errors instead of attempted (issue #1814).
_UNDRIVABLE_STEP_TYPES = frozenset(
    {"external", "external_done", "progress", "progress_done"}
)
_RECONFIGURE_SUCCESS_REASONS = frozenset(
    {
        "reauth_successful",
        "reconfigure_successful",
    }
)


class ReconfigureStatus(StrEnum):
    """Outcome vocabulary for the ``ha_set_integration`` reconfigure mode.

    Ordered by how far the request got. Everything from ``APPLIED_*`` onward
    means Home Assistant committed the change, so a caller must not blindly
    retry those.
    """

    #: Read-only preflight: validated, nothing started.
    PREVIEW = "preview"
    #: A confirmation was required and none was supplied.
    CONFIRMATION_REQUIRED = "confirmation_required"
    #: The supplied confirm_token no longer matches the entry's state.
    STALE_PREFLIGHT = "stale_preflight"
    #: The flow was aborted before any value reached Home Assistant.
    FLOW_ABORTED_BEFORE_APPLY = "flow_aborted_before_apply"
    #: Home Assistant rejected the change; nothing was committed.
    APPLY_FAILED = "apply_failed"
    #: Committed, but the flow never consumed every supplied value.
    APPLIED_BUT_INCOMPLETE = "applied_but_incomplete"
    #: Committed, and the entry no longer carries the identity it had.
    APPLIED_IDENTITY_MISMATCH = "applied_identity_mismatch"
    #: Committed, identity intact, and the entry reloaded cleanly.
    APPLIED_AND_VERIFIED = "applied_and_verified"
    #: Committed, but the result could not be fully verified.
    APPLIED_BUT_UNVERIFIED = "applied_but_unverified"


#: Statuses that mean Home Assistant already committed the change. Callers use
#: this to decide whether aborting a flow or retrying is still safe.
POST_COMMIT_STATUSES = frozenset(
    {
        ReconfigureStatus.APPLIED_BUT_INCOMPLETE,
        ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        ReconfigureStatus.APPLIED_AND_VERIFIED,
        ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
    }
)


def _parse_flow_api_error(
    api_error: HomeAssistantAPIError,
) -> dict[str, Any]:
    """Extract structured field-level info from an HA flow 4xx response.

    Home Assistant returns voluptuous validation failures during flow
    submission as either:

    - ``{"message": "User input malformed: extra keys not allowed @ data['name']"}``
      (raised before form validation, e.g. unknown field in payload)
    - ``{"errors": {"base": "..."}, "description_placeholders": {...}}``
      (per-field errors after voluptuous validation succeeds)
    - Free-form text (when the body isn't JSON).

    Returns a dict with at least:
      - ``message``: the most informative human-readable string we found.
      - ``field_errors``: dict of field-name -> error code/message, when
        the body contained an ``errors`` map. Empty dict otherwise.
      - ``raw``: the response_data dict (or ``None``) for diagnostics.
    """
    body = api_error.response_data or {}
    field_errors: dict[str, Any] = {}
    message_parts: list[str] = []

    if isinstance(body, dict):
        errors_field = body.get("errors")
        if isinstance(errors_field, dict):
            field_errors = {
                key: val for key, val in errors_field.items() if isinstance(key, str)
            }

        # HA's stock 400 carries a `message` key with the voluptuous detail.
        msg = body.get("message")
        if isinstance(msg, str) and msg.strip():
            message_parts.append(msg.strip())

        # description_placeholders sometimes carry the human-readable error.
        placeholders = body.get("description_placeholders")
        if isinstance(placeholders, dict):
            for key, val in placeholders.items():
                if isinstance(val, str) and val.strip():
                    message_parts.append(f"{key}: {val.strip()}")

    if not message_parts:
        # Fall back to the wrapper exception message ("API error: 400 - ...").
        message_parts.append(str(api_error))

    return {
        "message": " | ".join(dict.fromkeys(message_parts)),  # de-dupe, preserve order
        "field_errors": field_errors,
        "raw": body if isinstance(body, dict) else None,
    }


def _build_flow_error_context(
    flow_id: str,
    status_code: int,
    helper_type: str | None,
    menu_choice: str | None,
    current_step: dict[str, Any] | None,
    submitted: dict[str, Any] | None,
    parsed_raw: dict[str, Any] | None,
) -> dict[str, Any]:
    context: dict[str, Any] = {"flow_id": flow_id, "status_code": status_code}
    if helper_type:
        context["helper_type"] = helper_type
    if menu_choice:
        context["menu_choice"] = menu_choice
    if current_step is not None:
        context["step_id"] = current_step.get("step_id")
    if submitted is not None:
        context["submitted_keys"] = sorted(submitted.keys())
    if parsed_raw is not None:
        context["response_body"] = parsed_raw
    return context


async def _raise_flow_api_error(
    api_error: HomeAssistantAPIError,
    *,
    client: Any,
    flow_id: str,
    helper_type: str | None,
    menu_choice: str | None,
    current_step: dict[str, Any] | None,
    submitted: dict[str, Any] | None,
    is_reconfigure: bool = False,
) -> NoReturn:
    """Translate an HA 4xx during a flow submit into a structured ToolError.

    For 400/422 responses, parses ``response_data`` for field-level info
    via ``_parse_flow_api_error``. When the body is unstructured (no
    ``errors`` map), attaches a ``data_schema`` so the caller has actionable
    information.

    The schema is the live step's own whenever it carries one, so a later
    form's rejection shows that form; a fresh introspection flow is the
    fallback for a schema-less step. ``is_reconfigure`` skips that fallback
    too (``helper_type`` carries the integration domain there) and makes the
    prose name the integration instead of a helper, because this path also
    serves ``ha_set_integration(reconfigure=True)``.

    Always raises ``ToolError`` — never returns.
    """
    parsed = _parse_flow_api_error(api_error)
    field_errors = parsed["field_errors"]
    status_code = api_error.status_code or 0

    context = _build_flow_error_context(
        flow_id,
        status_code,
        helper_type,
        menu_choice,
        current_step,
        submitted,
        parsed["raw"],
    )

    suggestions: list[str] = []
    message: str

    current_schema = None
    if current_step is not None:
        step_schema = current_step.get("data_schema")
        if isinstance(step_schema, list):
            current_schema = step_schema

    if current_schema is not None or is_reconfigure:
        # The live step is the form HA rejected: a fresh introspection would
        # show the helper's first setup form instead, a different form in a
        # multi-step, options or reconfigure flow.
        schema = current_schema
    else:
        info = await fetch_helper_flow_info(client, helper_type, menu_choice)
        schema = info.get("schema")
    # An options/reconfigure step's schema carries the persisted values in
    # description.suggested_value — under redact_secrets those must not ride
    # into the error context verbatim (#2157). Deep-copies, so the live
    # flow_result is untouched.
    if schema is not None and redaction_enabled():
        schema = redact_flow_schema(schema)

    # Issue #1149: the schema rides with field_errors too — what failed plus
    # what's accepted is enough for self-correction.
    if schema is not None:
        context["data_schema"] = schema
    if field_errors:
        # Structured field errors — tell the caller which fields failed.
        # HA names fields only when the caller's input failed its schema.
        code = ErrorCode.VALIDATION_INVALID_PARAMETER
        context["field_errors"] = field_errors
        readable = ", ".join(f"{k}: {v}" for k, v in field_errors.items())
        subject = (
            f"{helper_type} reconfigure validation"
            if is_reconfigure and helper_type
            else "Reconfigure validation"
            if is_reconfigure
            else "Helper validation"
        )
        message = f"{subject} failed — {readable}"
        suggestions.append(
            "Fix the field(s) listed in 'field_errors' and retry the call."
        )
    else:
        # Unstructured — attach the data_schema so the LLM has something to use.
        code = ErrorCode.SERVICE_CALL_FAILED
        subject = (
            f"{helper_type} reconfigure"
            if is_reconfigure and helper_type
            else "reconfigure"
            if is_reconfigure
            else helper_type or "flow"
        )
        message = (
            f"Home Assistant rejected the {subject} request "
            f"({status_code}): {parsed['message']}"
        )
        if schema is not None:
            suggestions.append(
                "Inspect 'data_schema' in this error to see the fields HA expects, "
                "then retry with a corrected config."
            )

    if is_reconfigure:
        context["status"] = ReconfigureStatus.APPLY_FAILED

    raise_tool_error(
        create_error_response(
            code,
            message,
            suggestions=suggestions,
            context=context,
        )
    )


async def _submit_step(
    submit_fn: Any,
    flow_id: str,
    payload: dict[str, Any],
    *,
    client: Any,
    helper_type: str | None,
    last_menu_choice: str | None,
    current_step: dict[str, Any],
    is_reconfigure: bool = False,
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(submit_fn(flow_id, payload), timeout=20.0)
    except HomeAssistantAPIError as api_err:
        if api_err.status_code in (400, 422):
            await _raise_flow_api_error(
                api_err,
                client=client,
                flow_id=flow_id,
                helper_type=helper_type,
                menu_choice=last_menu_choice,
                current_step=current_step,
                submitted=payload,
                is_reconfigure=is_reconfigure,
            )
        if is_reconfigure and (api_err.status_code or 0) >= 500:
            # A 5xx is the HTTP spelling of "no answer came back". A 504 from
            # a proxy in front of Home Assistant means the upstream WAS
            # reached and simply did not answer in time, so the submit — the
            # call that probes, commits and reloads — may already have
            # committed. Without a status this re-raised bare and the generic
            # handler in _run_reconfigure_flow aborted a possibly-committed
            # flow, which is exactly what POST_COMMIT_STATUSES exists to
            # suppress. 4xx stays a re-raise: HA parsed the request and
            # answered about it (rejection, auth, unknown flow).
            _raise_reconfigure_no_answer(
                api_err,
                flow_id=flow_id,
                payload=payload,
                current_step=current_step,
                error_code=ErrorCode.SERVICE_CALL_FAILED,
                detail=f"HTTP {api_err.status_code}",
            )
        raise
    except no_answer_errors() as no_answer:
        # Every class here is evidence that NO ANSWER came back — see
        # _NO_ANSWER_ERRORS in rest_client.py — as opposed to Home Assistant
        # answering with a rejection (which the 400/422 branch above handles).
        # The submit is the call that probes, commits and reloads, so losing
        # the answer to it is precisely the applied_but_unverified ambiguity:
        # HA may already have committed.
        #
        # Widening beyond TimeoutError matters because rest_client funnels
        # EVERY httpx transport failure (ConnectError, TimeoutException,
        # HTTPError) into HomeAssistantConnectionError, so an ordinary
        # connection reset after the POST reached HA took the old re-raise
        # path: it reached the caller with no status at all, and the generic
        # handler upstream aborted a flow that may have committed. A caller
        # reads a bare connection error as "nothing happened" and retries.
        #
        # Carrying the post-commit status also suppresses that abort, since
        # _run_reconfigure_flow gates it on POST_COMMIT_STATUSES.
        if is_reconfigure:
            _raise_reconfigure_no_answer(
                no_answer,
                flow_id=flow_id,
                payload=payload,
                current_step=current_step,
                error_code=ErrorCode.TIMEOUT_OPERATION
                if isinstance(no_answer, TimeoutError)
                else ErrorCode.SERVICE_CALL_FAILED,
                detail=type(no_answer).__name__,
            )
        raise


def _raise_reconfigure_no_answer(
    cause: BaseException,
    *,
    flow_id: str,
    payload: dict[str, Any],
    current_step: dict[str, Any],
    error_code: ErrorCode,
    detail: str,
) -> NoReturn:
    """Raise ``applied_but_unverified`` for a submit whose answer was lost.

    Carrying the post-commit status is what suppresses the flow abort, since
    ``_run_reconfigure_flow`` gates that on ``POST_COMMIT_STATUSES``.
    """
    raise_tool_error(
        create_error_response(
            error_code,
            "Reconfigure flow submission got no answer from Home "
            f"Assistant ({detail}); the change may have been applied",
            suggestions=[
                "Do not retry automatically. Inspect the config entry in Home Assistant before attempting rollback."
            ],
            context={
                "flow_id": flow_id,
                "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                "submitted_keys": sorted(payload),
                "details": current_step,
            },
        )
    )


__all__ = [
    "POST_COMMIT_STATUSES",
    "ReconfigureStatus",
    "_RECONFIGURE_SUCCESS_REASONS",
    "_UNDRIVABLE_STEP_TYPES",
    "logger",
]
