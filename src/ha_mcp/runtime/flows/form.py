"""Step-local configuration values and schema submission orchestration."""

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from ..errors import raise_failure as raise_tool_error
from .menu import _MENU_SELECTION_KEY_ORDER
from .schema import (
    _CLEARED as _CLEARED,
)
from .schema import (
    _MENU_SELECTION_KEYS as _MENU_SELECTION_KEYS,
)
from .schema import (
    _MISSING_DEFAULT as _MISSING_DEFAULT,
)
from .schema import (
    _NO_SUBMISSION as _NO_SUBMISSION,
)
from .schema import (
    _PER_STEP_VALUES_KEY as _PER_STEP_VALUES_KEY,
)
from .schema import (
    _RESERVED_CONFIG_KEYS as _RESERVED_CONFIG_KEYS,
)
from .schema import (
    _auto_confirm_form_payload as _auto_confirm_form_payload,
)
from .schema import (
    _clears_by_omission as _clears_by_omission,
)
from .schema import (
    _consume_declared_section as _consume_declared_section,
)
from .schema import (
    _consume_form_schema as _consume_form_schema,
)
from .schema import (
    _consume_leaf_field as _consume_leaf_field,
)
from .schema import (
    _consume_section_schema as _consume_section_schema,
)
from .schema import (
    _current_value_backfill as _current_value_backfill,
)
from .schema import (
    _edit_mode_submission as _edit_mode_submission,
)
from .schema import (
    _field_default_value as _field_default_value,
)
from .schema import (
    _ignored_keys_warnings as _ignored_keys_warnings,
)
from .schema import (
    _is_redacted_value as _is_redacted_value,
)
from .schema import (
    _mark_consumed as _mark_consumed,
)
from .schema import (
    _record_ignored_section_keys as _record_ignored_section_keys,
)
from .schema import (
    _redeclared_field_submission as _redeclared_field_submission,
)
from .schema import (
    _required_section_defaults as _required_section_defaults,
)
from .schema import (
    _ReuseState as _ReuseState,
)
from .schema import (
    _schema_default_values as _schema_default_values,
)
from .schema import (
    _section_path as _section_path,
)
from .schema import (
    _step_owned_submission_value as _step_owned_submission_value,
)
from .schema import (
    _strip_cleared as _strip_cleared,
)
from .schema import (
    _success_warnings as _success_warnings,
)
from .schema import (
    iter_schema_fields as iter_schema_fields,
)


def _extract_schema_field_names(data_schema: Any) -> set[str] | None:
    """Extract the set of field names declared by a step's data_schema.

    HA returns data_schema as a list of {name, selector, required, ...} dicts.
    Nested leaf names are included; section-container names are omitted.
    Returns ``None`` when the schema is absent or not a list (signalling
    the caller to fall back to legacy submit-all behaviour). Returns a
    (possibly empty) set when the schema is present and parseable.
    """
    if not isinstance(data_schema, list):
        return None
    names: set[str] = set()
    for field in iter_schema_fields(data_schema):
        name = field.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _consume_all_remaining_keys(
    remaining_config: dict[str, Any],
    consumed_config_keys: set[str] | None,
    reuse_state: _ReuseState | None,
) -> dict[str, Any]:
    """Submit every non-menu key, for a step whose schema HA did not send.

    Without field names there is nothing to filter on, so the whole config is
    dumped into this one submit and cleared out of ``remaining_config``. Each
    consumed key is still recorded by leaf name, so a later step that *does*
    arrive with a schema declaring one of them can be filled from the record
    rather than submitted without it.

    In a cyclic-menu flow the config legitimately carries values destined
    for several branches, so a schemaless step mid-loop sweeping them all up
    is far likelier to misfire ("extra keys not allowed" attributed to the
    wrong step) — a warning is surfaced when this fires while menu
    selections are still queued, so the sweep is at least visible.
    """
    form_data: dict[str, Any] = {}
    for key in list(remaining_config.keys()):
        if key in _RESERVED_CONFIG_KEYS:
            continue
        value = remaining_config.pop(key)
        form_data[key] = value
        _mark_consumed(consumed_config_keys, "", key)
        if reuse_state is not None:
            reuse_state.record("", key, value, scoped_only=False)
    queued = {
        key: remaining_config[key]
        for key in _MENU_SELECTION_KEY_ORDER
        if key in remaining_config
    }
    if form_data and queued and reuse_state is not None:
        reuse_state.notes.append(
            "A step without a schema consumed every remaining config key "
            f"({', '.join(sorted(form_data))}) while menu selection(s) "
            f"{queued!r} were still queued — values intended for later "
            "branches may have been submitted to this step instead"
        )
    return form_data


def validate_step_values(config: dict[str, Any]) -> None:
    """Reject a malformed ``step_values`` directive before the walk starts.

    The reserved key is consumed as a directive, so a malformed one is invisible
    to ignored-key reporting: the walk would complete, report success, and have
    applied nothing the caller asked for. A caller error is a tool-level
    failure, so it raises rather than warns (CodeRabbit review, issue #2254).

    Accepted: a dict of ``step_id -> entry``, where an entry is a dict of field
    values, or a LIST of such dicts consumed one per encounter of that step.
    An empty dict inside such a list is a per-encounter no-op — that encounter
    behaves as it would with no directive at all — but an entry that applies
    nothing anywhere is rejected.
    """
    # Key PRESENCE is the test, not truthiness: an explicit ``None`` is a
    # caller who meant to pass a directive and got the shape wrong, and the
    # reserved key hides it from ignored-key reporting, so returning early on
    # it would let the walk apply the rest and report a clean success.
    if _PER_STEP_VALUES_KEY not in config:
        return
    directive = config[_PER_STEP_VALUES_KEY]

    example = "{'<step_id>': {'<field>': <value>}}"
    if not isinstance(directive, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{_PER_STEP_VALUES_KEY}' must be an object keyed by step id, "
                f"got {type(directive).__name__}",
                suggestions=[f"Pass {_PER_STEP_VALUES_KEY}={example}."],
                # TYPE, never the value. A rejected directive can carry a
                # credential the caller is submitting for the FIRST time, which
                # Home Assistant does not hold yet — so no read-back has
                # harvested it and RedactSecretsMiddleware cannot scrub it even
                # when enabled. raise_tool_error serialises this whole response
                # into the exception message, and @log_tool_usage records that
                # as error_message from inside the tool, where only
                # `parameters` are masked: the value would reach plaintext
                # mcp_usage.jsonl (Patch76 review, issue #2254). The shape is
                # what diagnoses a malformed directive anyway.
                context={"received_type": type(directive).__name__},
            )
        )

    # Report the entry's POSITION, not the caller's step id: these errors reach
    # the usage log unmasked (see the note above), and a step id is a
    # caller-controlled key. The position still says which entry is wrong and
    # cannot carry a value they typed (CodeRabbit review, issue #2254).
    #
    # Scoped to VALUES deliberately. Caller-supplied field NAMES do reach the
    # same logged path — ``step_values.<step_id>.<field>`` goes into
    # ``ignored_config_keys``, which ``_unconsumed_reconfigure_keys`` unions
    # into the reconfigure abort error — and that is kept, because naming the
    # field is the whole diagnostic for a typo and a name is not the secret a
    # value is (Patch76 review, issue #2254).
    for index, entry in enumerate(directive.values(), start=1):
        where = f"{_PER_STEP_VALUES_KEY} entry #{index}"
        entries = entry if isinstance(entry, list) else [entry]
        if any(not isinstance(item, dict) for item in entries):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{where} must be an object of field values, or a list "
                    "of them for a step the flow presents more than once",
                    suggestions=[
                        f"Pass {_PER_STEP_VALUES_KEY}={example}, or a list of "
                        "those objects to supply one per encounter.",
                    ],
                    context={
                        "entry_index": index,
                        "entry_types": [type(item).__name__ for item in entries],
                    },
                )
            )
        # An entry that can apply NOTHING AT ALL is a caller mistake in the
        # same way a malformed one is, and it was reported inconsistently: a
        # bare {} left a leftover the warning named, while [] and [{}] were
        # popped at their first encounter and vanished, so the walk reported a
        # clean success for a directive that did nothing (Patch76 review).
        #
        # An empty object INSIDE a longer list is deliberately not that: it
        # means "leave this encounter as it would be without the directive",
        # which nothing else expresses — omitting the step affects every
        # encounter, and {"field": None} is an explicit clear rather than a
        # fallback. So the test spans the whole entry rather than each item.
        if not any(entries):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{where} supplies no field values, so it would apply nothing",
                    suggestions=[
                        "Name at least one field for the step, or drop the entry.",
                    ],
                    context={"entry_index": index, "entry_count": len(entries)},
                )
            )

    if not directive:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{_PER_STEP_VALUES_KEY}' is empty, so it would apply nothing",
                suggestions=[f"Name a step: {_PER_STEP_VALUES_KEY}={example}."],
            )
        )


@contextmanager
def _step_values_applied(
    remaining_config: dict[str, Any],
    current_step: dict[str, Any],
    ignored_config_keys: set[str] | None,
    reuse_state: _ReuseState | None,
) -> Iterator[None]:
    """Overlay this step's entry from ``step_values`` for the duration.

    The flat config dict is keyed by field name alone, so a field two steps
    declare could only ever carry one value for both. Inside this block the
    step's own entry SHADOWS the flat value, and on the way out the flat value
    is restored so a later step still sees it — addressing one step therefore
    never spends the caller's flat value, and a step nobody addresses behaves
    exactly as it did before.

    An overlaid key the step turns out not to declare is dropped rather than
    left behind: it belongs to this step, so it must not leak into a later
    one or be reported as a field no step declared.
    """
    overlay = remaining_config.get(_PER_STEP_VALUES_KEY)
    step_id = current_step.get("step_id")
    if not isinstance(overlay, dict):
        yield
        return

    raw = overlay.get(step_id)
    # A LIST is consumed one entry per encounter, its un-consumed tail
    # replacing the key until it runs dry — the same shape ``next_step_id``
    # uses for a menu the flow revisits, and the only one that can express a
    # loop. A single dict applies once and is spent, which is why taking the
    # warning's advice used to reach FEWER encounters than the flat key it
    # replaces. The caller's list object is never mutated.
    is_list = isinstance(raw, list)
    head = (raw[0] if raw else None) if is_list else raw
    values = head if isinstance(head, dict) and head else {}
    if not values and not is_list:
        yield
        return

    shadowed = {k: remaining_config[k] for k in values if k in remaining_config}
    if values:
        remaining_config.update(copy.deepcopy(values))
        if reuse_state is not None:
            reuse_state.step_scoped |= set(values)
    try:
        yield
    finally:
        if values and reuse_state is not None:
            reuse_state.step_scoped -= set(values)
        for key in values:
            # Still present = this step's schema never declared it. A field
            # scoped to one step applies nowhere else, so it would otherwise
            # vanish without ever being submitted or reported — report it
            # under its own path before dropping it. Checked BEFORE the
            # shadowed flat values go back, or a restored flat key would look
            # like an unconsumed overlay one.
            if key in remaining_config and ignored_config_keys is not None:
                ignored_config_keys.add(f"{_PER_STEP_VALUES_KEY}.{step_id}.{key}")
            remaining_config.pop(key, None)
        remaining_config.update(shadowed)
        # Spend this encounter. A list leaves its tail for the next visit; a
        # dict is spent outright. What survives the whole walk is reported
        # rather than silently doing nothing, the same way an un-consumed
        # menu selection is.
        tail = raw[1:] if isinstance(raw, list) else []
        if tail:
            overlay[step_id] = tail
        else:
            overlay.pop(step_id, None)
        if not overlay:
            remaining_config.pop(_PER_STEP_VALUES_KEY, None)


def _handle_form_step(
    flow_id: str,
    current_step: dict[str, Any],
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str] | None = None,
    consumed_config_keys: set[str] | None = None,
    reuse_state: _ReuseState | None = None,
    *,
    keep_current_values: bool = False,
) -> dict[str, Any]:
    """Validate a form step and return form data to submit.

    When the step's ``data_schema`` is provided, pops ONLY the keys declared
    in that schema from ``remaining_config`` (mutating it) so any unconsumed
    keys remain available for subsequent steps. Menu selection keys are never
    submitted. Fields declared inside a section are grouped under the section
    key; callers may provide them flat or inside an explicit section dict.

    Caller-supplied values are recorded in ``reuse_state``. A field this step
    declares but the caller named no key for here is filled per
    :func:`_redeclared_field_submission`: omitted when the schema carries a
    ``"default"`` or the field is optional, submitted from the step's own
    suggestion or constant, and otherwise — required, no default, no value of
    its own — resubmitted once from an earlier step's caller value with a
    warning. Nothing is injected into an optional field, or into a section
    neither marked required nor named by the caller.

    ``keep_current_values`` switches that last sentence off for the flows that
    edit an existing object — options, reconfigure, subentry reconfigure. Their
    steps arrive pre-filled with the stored values and the UI's save posts all
    of them back, so a declared field the caller left out is submitted with the
    step's own value wherever the step supplies one (issue #2254); a field the
    caller set to ``None`` is a clear, submitted as an omission only where the
    field has no schema default to be substituted in its place (see
    :func:`_consume_leaf_field`). It takes a ``reuse_state`` to work: the record is what keeps
    a backfill off a path the caller already filled in this same step.

    When ``data_schema`` is absent (HA didn't tell us field names), falls
    back to legacy behaviour: submit all non-menu keys and clear them. This
    keeps single-step flows working when HA omits the schema.

    Raises ToolError on validation errors.
    """
    if current_step.get("errors"):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Form validation failed",
                suggestions=["Fix the field errors and retry with corrected values"],
                context={
                    "flow_id": flow_id,
                    "step_id": current_step.get("step_id"),
                    "errors": current_step["errors"],
                    "data_schema": current_step.get("data_schema"),
                },
            )
        )

    if reuse_state is not None:
        reuse_state.begin_step(current_step.get("step_id"))

    data_schema = current_step.get("data_schema")
    if not isinstance(data_schema, list):
        return _consume_all_remaining_keys(
            remaining_config, consumed_config_keys, reuse_state
        )

    with _step_values_applied(
        remaining_config, current_step, ignored_config_keys, reuse_state
    ):
        return _strip_cleared(
            _consume_form_schema(
                data_schema,
                remaining_config,
                ignored_config_keys,
                consumed_config_keys,
                "",
                reuse_state,
                keep_current_values=keep_current_values,
            )
        )
