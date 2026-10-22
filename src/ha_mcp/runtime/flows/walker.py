"""Shared bounded config/options/subentry flow walkers."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from ..errors import raise_failure as raise_tool_error
from .form import (
    _MENU_SELECTION_KEYS,
    _PER_STEP_VALUES_KEY,
    _auto_confirm_form_payload,
    _handle_form_step,
    _ReuseState,
    _success_warnings,
    validate_step_values,
)
from .introspect import _FlowType
from .menu import (
    _flow_step_budget,
    _handle_menu_step,
)
from .submission import (
    _RECONFIGURE_SUCCESS_REASONS as _RECONFIGURE_SUCCESS_REASONS,
)
from .submission import (
    _UNDRIVABLE_STEP_TYPES as _UNDRIVABLE_STEP_TYPES,
)
from .submission import (
    POST_COMMIT_STATUSES as POST_COMMIT_STATUSES,
)
from .submission import (
    ReconfigureStatus as ReconfigureStatus,
)
from .submission import (
    _build_flow_error_context as _build_flow_error_context,
)
from .submission import (
    _parse_flow_api_error as _parse_flow_api_error,
)
from .submission import (
    _raise_flow_api_error as _raise_flow_api_error,
)
from .submission import (
    _raise_reconfigure_no_answer as _raise_reconfigure_no_answer,
)
from .submission import (
    _submit_step as _submit_step,
)
from .submission import (
    logger as logger,
)


def _empty_forms_suggestions(supplied_keys: list[str]) -> list[str]:
    """Guidance for a flow that consumed none of the caller's keys.

    ``step_values`` is a directive rather than a field, so "check the field
    names" is the wrong advice when it is what the caller supplied: the likely
    mistake is a step_id the flow never presents, not a misspelled field.
    """
    if _PER_STEP_VALUES_KEY in supplied_keys:
        return [
            "Check each step_values step_id against the steps this flow "
            "actually presents — a step_id the flow never reaches applies "
            "nothing — and check the field names inside each entry with "
            "ha_get_integration(entry_id=..., include_schema=True).",
        ]
    return [
        "Check the field names against the flow's data_schema — "
        "ha_get_integration(entry_id=..., include_schema=True) "
        "shows the accepted fields — then retry with corrected keys.",
    ]


def _finish_flow_entry(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    supplied_keys: list[str],
    saw_form_step: bool,
    any_form_key_consumed: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Build the CREATE_ENTRY success response, or raise on total key miss.

    When the flow presented at least one form step, the caller supplied
    config keys, and NONE were consumed by any form (typos / wrong field
    names), the flow was walked on empty forms and HA saved form defaults —
    not the caller's values. A success + warning there reads as "done" to an
    LLM caller, so fail loudly with the schema route instead. Partial
    consumption — and flows that complete without any form step (instant
    creates) — keep the established success + warnings contract.
    """
    if supplied_keys and saw_form_step and not any_form_key_consumed:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Flow completed without consuming any of the supplied "
                "config keys — every form step was submitted empty, so "
                "the flow saved its defaults, not your values",
                suggestions=_empty_forms_suggestions(supplied_keys),
                context={
                    "flow_id": flow_id,
                    "supplied_keys": supplied_keys,
                    "details": current_step,
                },
            )
        )
    response: dict[str, Any] = {"success": True, "entry": current_step}
    warnings = _success_warnings(ignored_config_keys, remaining_config, reuse_state)
    if warnings:
        response["warnings"] = warnings
    return response


def _flow_failure_context(
    flow_id: str, current_step: dict[str, Any], *, is_reconfigure: bool
) -> dict[str, Any]:
    context: dict[str, Any] = {"flow_id": flow_id, "details": current_step}
    if is_reconfigure:
        context["status"] = ReconfigureStatus.APPLY_FAILED
    return context


def _raise_flow_abort(
    flow_id: str, current_step: dict[str, Any], *, is_reconfigure: bool = False
) -> NoReturn:
    """Raise the structured error for an ABORT flow step."""
    reason = current_step.get("reason")
    abort_suggestions: list[str] = []
    if reason in ("already_configured", "single_instance_allowed"):
        # Common benign aborts on the add-integration path (#1814): give the
        # caller a route to the existing entry instead of a bare failure.
        abort_suggestions.append(
            "The integration is already set up — use "
            "ha_get_integration() to find the existing entry."
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Flow aborted: {reason}",
            suggestions=abort_suggestions or None,
            context=_flow_failure_context(
                flow_id, current_step, is_reconfigure=is_reconfigure
            ),
        )
    )


def _unconsumed_reconfigure_keys(
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
) -> list[str]:
    """List caller keys that a reconfigure flow never consumed.

    ``remaining_config`` starts as a copy of the caller's config and is only
    ever popped from, or reassigned at a key it already holds, so every key
    still in it is a caller key.
    """
    return sorted(ignored_config_keys | set(remaining_config))


def _reconfigure_success_response(
    current_step: dict[str, Any],
    *,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Build the common successful reconfigure response."""
    response: dict[str, Any] = {
        # Past tense, matching the "created" this walker's sibling emits. Only
        # set_config_subentry reads this; the ha_set_integration reconfigure
        # surface builds its own response and reports operation "reconfigure"
        # on both the preview and the applied leg.
        "success": True,
        "operation": "reconfigured",
        "flow_result": current_step,
    }
    warnings = _success_warnings(ignored_config_keys, remaining_config, reuse_state)
    if warnings:
        response["warnings"] = warnings
    return response


def _reconfigure_abort_result(
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    flow_id: str,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any] | None:
    """Translate a successful reconfigure abort into a walker result."""
    if (
        not is_reconfigure
        or current_step.get("reason") not in _RECONFIGURE_SUCCESS_REASONS
    ):
        return None
    unresolved = _unconsumed_reconfigure_keys(ignored_config_keys, remaining_config)
    if unresolved:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfigure flow completed without consuming all supplied "
                "configuration values",
                suggestions=[
                    "Inspect the existing entry state before taking any further "
                    "action; Home Assistant may already have applied part of the "
                    "requested change.",
                ],
                context={
                    "flow_id": flow_id,
                    "status": ReconfigureStatus.APPLIED_BUT_INCOMPLETE,
                    "unconsumed_config_keys": unresolved,
                    "details": current_step,
                },
            )
        )
    return _reconfigure_success_response(
        current_step,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )


def _handle_abort_step(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Handle normal aborts and successful reconfigure aborts."""
    reconfigure_result = _reconfigure_abort_result(
        current_step,
        is_reconfigure=is_reconfigure,
        flow_id=flow_id,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )
    if reconfigure_result is not None:
        return reconfigure_result
    _raise_flow_abort(flow_id, current_step, is_reconfigure=is_reconfigure)
    raise AssertionError("unreachable after _raise_flow_abort")


def _flow_form_payload(
    current_step: dict[str, Any],
    *,
    flow_id: str,
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str],
    reuse_state: _ReuseState,
    keep_current_values: bool,
) -> tuple[dict[str, Any], bool]:
    """Build one generic flow form payload.

    Returns the payload and whether the step consumed at least one caller key
    — not which ones; ``remaining_config`` and ``ignored_config_keys`` carry
    that. A value the step backfills under ``keep_current_values`` is the
    step's own data, so it never counts as a consumed caller key.
    """
    consumed_form_keys: set[str] = set()
    form_data = _auto_confirm_form_payload(current_step)
    if form_data is None:
        form_data = _handle_form_step(
            flow_id,
            current_step,
            remaining_config,
            ignored_config_keys,
            consumed_form_keys,
            reuse_state,
            keep_current_values=keep_current_values,
        )
    return form_data, bool(consumed_form_keys)


def _handle_flow_create_entry(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    supplied_keys: list[str],
    saw_form_step: bool,
    any_form_key_consumed: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Finish a normal flow, but never treat create_entry as reconfigure success."""
    if is_reconfigure:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfigure flow created a new config entry instead of "
                "updating the existing entry",
                suggestions=[
                    "Inspect Home Assistant for a duplicate entry before "
                    "retrying; do not create another entry automatically."
                ],
                context={
                    "flow_id": flow_id,
                    "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                    "details": current_step,
                },
            )
        )
    return _finish_flow_entry(
        flow_id,
        current_step,
        supplied_keys=supplied_keys,
        saw_form_step=saw_form_step,
        any_form_key_consumed=any_form_key_consumed,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )


async def _handle_flow_steps(
    client: Any,
    flow_id: str,
    initial_step: dict[str, Any],
    config: dict[str, Any],
    submit_fn: Any = None,
    helper_type: str | None = None,
    *,
    is_reconfigure: bool = False,
    keep_current_values: bool = False,
    complete_snapshot: bool = False,
) -> dict[str, Any]:
    """Walk a multi-step config flow handling menu and form steps.

    The step budget comes from :func:`_flow_step_budget`: a base of 10
    steps plus two per caller-supplied menu selection.

    HA flows can present steps in sequence:
    - ``menu``: caller supplies selection via ``group_type``/``next_step_id`` key
    - ``form``: caller supplies field values; aborts immediately on validation errors
    - ``create_entry``: flow complete
    - ``abort``: flow terminated by HA

    Args:
        client: HomeAssistantClient instance
        flow_id: Flow ID from start_config_flow or start_options_flow
        initial_step: The first step returned by the flow start call
        config: Full caller-provided config dict. Menu selection keys are
            consumed by menu steps — a key may carry a single selection or a
            list of successive selections, consumed one per menu encounter
            (flows can revisit a menu after each branch; issue #2116).
            Remaining keys are submitted on the first
            form step whose schema declares them or, when HA omits the schema,
            on the first form step outright. A later step that redeclares an
            already-consumed field gets that value resubmitted once, with a
            warning, where the step marks it required and supplies neither a
            default nor a value of its own (see
            :func:`_redeclared_field_submission`).
        submit_fn: Async function to submit a step. Defaults to
            client.submit_config_flow_step (create). Pass
            client.submit_options_flow_step for options (update) flows.
        helper_type: Optional helper type (e.g. ``"statistics"``). An
            unstructured HA 4xx carries the rejected step's own data_schema
            in its error context; when that step has none, the helper's
            setup form is introspected under this type (never under
            ``is_reconfigure``, where the value only names the integration
            in error prose).
        keep_current_values: Whether this flow edits an existing object
            (options, reconfigure, subentry reconfigure) rather than creating
            one. Its steps arrive pre-filled with the stored values and the HA
            UI's save posts every box back, so a declared field the caller
            named no key for is submitted with the step's own value instead of
            being dropped — which is what stopped a one-field options patch
            from resetting everything it did not mention (issue #2254). A
            field the caller explicitly set to ``None`` is a clear: for an
            optional field with no schema default that means consuming the key
            and omitting it, the only way such a field can be emptied;
            everything else submits the ``None`` so Home Assistant validates
            it, since omitting a defaulted field would substitute its static
            default rather than clear it. Backfilled
            values are the step's data, so they neither count towards the
            "consumed at least one caller key" test below nor satisfy the
            reconfigure "consumed EVERY key" one.
        complete_snapshot: A backup restore or recreation, whose ``submit_fn``
            checks the snapshot keys no form took before the form HA marks as
            last (``_OptionsFlowProgress``); skips the no-key-consumed check
            and the leftover-key warnings and reconfigure error, since a
            flow without a marked last form is verified by readback instead.
        is_reconfigure: Whether this is the official reconfigure flow — the
            same mode HA uses for reauth, so both ``reconfigure_successful``
            and ``reauth_successful`` count as its success aborts. In this
            mode ``create_entry`` is rejected as
            ``ReconfigureStatus.APPLIED_BUT_UNVERIFIED``, and a success abort
            is a success only when the flow consumed EVERY supplied config
            key — any leftover raises
            ``ReconfigureStatus.APPLIED_BUT_INCOMPLETE``, because Home
            Assistant has already committed whatever it did consume.

    Returns:
        ``{"success": True, "entry": result}`` for a normal flow, or
        ``{"success": True, "operation": "reconfigured", "flow_result":
        result}`` under ``is_reconfigure``. Both carry ``warnings``
        when SOME caller-supplied config keys were not declared by any flow
        step, or when a key had to be resubmitted to a later step that
        redeclared it. When the flow presented at least one form step and NONE
        of the supplied keys were consumed, raises ``VALIDATION_INVALID_PARAMETER``
        instead of reporting a misleading success — the flow completed on
        empty forms (defaults), applying nothing the caller asked for (see
        :func:`_finish_flow_entry`). Raises ToolError on any failure.
    """
    if submit_fn is None:
        submit_fn = client.submit_config_flow_step
    validate_step_values(config)
    remaining_config = dict(config)
    current_step = initial_step
    last_menu_choice: str | None = None
    consumed_menu_selections: list[str] = []
    consumed_menu_selection_keys: list[str] = []
    ignored_config_keys: set[str] = set()
    reuse_state = _ReuseState()
    # A snapshot's submit_fn checks the keys no form took (_OptionsFlowProgress),
    # and may legitimately submit no form field.
    supplied_keys = (
        []
        if complete_snapshot
        else sorted(k for k in config if k not in _MENU_SELECTION_KEYS)
    )
    saw_form_step = False
    any_form_key_consumed = False
    max_steps = _flow_step_budget(config)

    for step_num in range(max_steps):
        result_type = current_step.get("type")

        if result_type == _FlowType.CREATE_ENTRY:
            # A snapshot's keys no form took are its fixed options (name, a
            # template's type), checked by submit_fn, not ignored input.
            return _handle_flow_create_entry(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                supplied_keys=supplied_keys,
                saw_form_step=saw_form_step,
                any_form_key_consumed=any_form_key_consumed,
                ignored_config_keys=set() if complete_snapshot else ignored_config_keys,
                remaining_config={} if complete_snapshot else remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.ABORT:
            return _handle_abort_step(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                ignored_config_keys=set() if complete_snapshot else ignored_config_keys,
                remaining_config={} if complete_snapshot else remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.MENU:
            menu_choice = _handle_menu_step(
                flow_id,
                current_step,
                remaining_config,
                consumed_menu_selections,
                consumed_menu_selection_keys,
            )
            last_menu_choice = menu_choice
            logger.debug(
                f"Flow step {step_num}: menu '{menu_choice}' "
                f"(step_id={current_step.get('step_id')})"
            )
            current_step = await _submit_step(
                submit_fn,
                flow_id,
                {"next_step_id": menu_choice},
                client=client,
                helper_type=helper_type,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )

        elif result_type == _FlowType.FORM:
            # _handle_form_step pops only the keys declared in the current
            # step's data_schema, leaving any other keys in remaining_config
            # for subsequent steps (HA can present multi-step forms, e.g.
            # statistics: user step then pick-characteristic step), and records
            # what it consumed for any later step that redeclares the same
            # field.
            saw_form_step = True
            form_data, consumed_any = _flow_form_payload(
                current_step,
                flow_id=flow_id,
                remaining_config=remaining_config,
                ignored_config_keys=ignored_config_keys,
                reuse_state=reuse_state,
                keep_current_values=keep_current_values,
            )
            if consumed_any:
                any_form_key_consumed = True
            logger.debug(
                f"Flow step {step_num}: form submit "
                f"(step_id={current_step.get('step_id')}, keys={list(form_data.keys())})"
            )
            current_step = await _submit_step(
                submit_fn,
                flow_id,
                form_data,
                client=client,
                helper_type=helper_type,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )

        elif result_type in _UNDRIVABLE_STEP_TYPES:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Flow reached a '{result_type}' step that cannot be "
                    "completed via MCP (browser/OAuth authorization or an "
                    "asynchronous provider step)",
                    suggestions=[
                        "Complete this flow in the Home Assistant UI "
                        "(Settings > Devices & Services)."
                    ],
                    context=_flow_failure_context(
                        flow_id, current_step, is_reconfigure=is_reconfigure
                    ),
                )
            )

        else:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_UNEXPECTED,
                    f"Unexpected flow result type: {result_type}",
                    context=_flow_failure_context(
                        flow_id, current_step, is_reconfigure=is_reconfigure
                    ),
                )
            )

    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Flow exceeded {max_steps} steps",
            context={
                "flow_id": flow_id,
                "max_steps": max_steps,
                "consumed_menu_selections": consumed_menu_selections,
                # Reconfigure-only keys: the reconfigure caller reads both to
                # decide whether to abort the pending flow. Adding them to
                # every flow's error shape would change the public contract of
                # the add-integration, options and helper paths for nothing.
                **(
                    {
                        "flow_budget_exhausted": True,
                        "status": ReconfigureStatus.FLOW_ABORTED_BEFORE_APPLY,
                    }
                    if is_reconfigure
                    else {}
                ),
            },
        )
    )


def _handle_subentry_create_entry(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Handle a subentry create result and reject it in reconfigure mode."""
    if is_reconfigure:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Config subentry reconfigure created a new entry instead "
                "of updating the existing entry",
                suggestions=[
                    "Inspect Home Assistant for a duplicate subentry before "
                    "retrying; do not create another entry automatically."
                ],
                context={
                    "flow_id": flow_id,
                    "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                    "details": current_step,
                },
            )
        )
    response: dict[str, Any] = {
        "success": True,
        "operation": "created",
        "flow_result": current_step,
    }
    warnings = _success_warnings(ignored_config_keys, remaining_config, reuse_state)
    if warnings:
        response["warnings"] = warnings
    return response


def _handle_subentry_abort(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Handle a subentry abort, enforcing consumed values after reconfigure."""
    reason = current_step.get("reason")
    reconfigure_result = _reconfigure_abort_result(
        current_step,
        is_reconfigure=is_reconfigure,
        flow_id=flow_id,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )
    if reconfigure_result is not None:
        return reconfigure_result
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Config subentry flow aborted: {reason}",
            context={"flow_id": flow_id, "details": current_step},
        )
    )
    raise AssertionError("unreachable after subentry flow abort")


async def _handle_config_subentry_flow_steps(
    client: Any,
    flow_id: str,
    initial_step: dict[str, Any],
    config: dict[str, Any],
    *,
    is_reconfigure: bool,
    keep_current_values: bool = False,
) -> dict[str, Any]:
    """Walk a config subentry flow and accept HA's reconfigure-success abort.

    Successful results include ``warnings`` when caller-supplied config keys
    were not declared by any flow step, or when a key was resubmitted to a
    later step that redeclared it. Fields redeclared by a later step are filled
    on the same terms as :func:`_handle_flow_steps`.

    ``is_reconfigure`` (set by ``set_config_subentry`` whenever a
    ``subentry_id`` is supplied) tightens that contract to match the
    integration reconfigure path: a success abort that left ANY supplied key
    unconsumed raises ``ReconfigureStatus.APPLIED_BUT_INCOMPLETE`` instead of
    returning success plus a warning. Home Assistant has already committed
    the keys it did consume, so a partial apply reported as success would
    read as "done" to an agent. This is a behaviour change for
    ``ha_config_set_helper(helper_type="config_subentry", subentry_id=...)``
    callers who previously relied on the warning.

    ``keep_current_values`` carries the same edit-mode contract as in
    :func:`_handle_flow_steps` (issue #2254): reconfiguring a subentry
    resubmits the step's own value for every declared field the caller named
    no key for, so a partial patch stops wiping the rest of the subentry.
    """
    validate_step_values(config)
    remaining_config = dict(config)
    current_step = initial_step
    last_menu_choice: str | None = None
    consumed_menu_selections: list[str] = []
    consumed_menu_selection_keys: list[str] = []
    ignored_config_keys: set[str] = set()
    reuse_state = _ReuseState()
    max_steps = _flow_step_budget(config)

    for step_num in range(max_steps):
        result_type = current_step.get("type")

        if result_type == _FlowType.CREATE_ENTRY:
            return _handle_subentry_create_entry(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                ignored_config_keys=ignored_config_keys,
                remaining_config=remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.ABORT:
            return _handle_subentry_abort(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                ignored_config_keys=ignored_config_keys,
                remaining_config=remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.MENU:
            menu_choice = _handle_menu_step(
                flow_id,
                current_step,
                remaining_config,
                consumed_menu_selections,
                consumed_menu_selection_keys,
            )
            last_menu_choice = menu_choice
            logger.debug(
                "Config subentry flow step %s: menu %s (step_id=%s)",
                step_num,
                menu_choice,
                current_step.get("step_id"),
            )
            current_step = await _submit_step(
                client.submit_config_subentry_flow_step,
                flow_id,
                {"next_step_id": menu_choice},
                client=client,
                helper_type=None,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )
            continue

        if result_type == _FlowType.FORM:
            form_data = _handle_form_step(
                flow_id,
                current_step,
                remaining_config,
                ignored_config_keys,
                reuse_state=reuse_state,
                keep_current_values=keep_current_values,
            )
            logger.debug(
                "Config subentry flow step %s: form submit (step_id=%s, keys=%s)",
                step_num,
                current_step.get("step_id"),
                sorted(form_data.keys()),
            )
            current_step = await _submit_step(
                client.submit_config_subentry_flow_step,
                flow_id,
                form_data,
                client=client,
                helper_type=None,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )
            continue

        if result_type in {"progress", "progress_done"}:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Config subentry flow requires an asynchronous progress step",
                    suggestions=[
                        "Complete the provider setup in Home Assistant so the external resource is available.",
                        "Retry the same ha_config_set_helper call after the resource is ready.",
                    ],
                    context={"flow_id": flow_id, "details": current_step},
                )
            )

        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                f"Unexpected config subentry flow result type: {result_type}",
                context={"flow_id": flow_id, "details": current_step},
            )
        )

    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Config subentry flow exceeded {max_steps} steps",
            context={
                "flow_id": flow_id,
                "max_steps": max_steps,
                "consumed_menu_selections": consumed_menu_selections,
                # Reconfigure-only keys: the reconfigure caller reads both to
                # decide whether to abort the pending flow. Adding them to
                # every flow's error shape would change the public contract of
                # the add-integration, options and helper paths for nothing.
                **(
                    {
                        "flow_budget_exhausted": True,
                        "status": ReconfigureStatus.FLOW_ABORTED_BEFORE_APPLY,
                    }
                    if is_reconfigure
                    else {}
                ),
            },
        )
    )
