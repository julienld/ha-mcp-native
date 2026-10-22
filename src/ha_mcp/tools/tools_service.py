"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

import logging
from typing import Annotated, Any, NamedTuple, NoReturn, NotRequired, TypedDict, cast

import httpx
from pydantic import ConfigDict, Field, SkipValidation, TypeAdapter, ValidationError

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors, legacy_errors_sync
from ..backends.remote_events import RemoteEvents
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import (
    ErrorCode,
    create_connection_error,
    create_entity_not_found_error,
    create_entity_unavailable_after_dispatch_error,
    create_error_response,
    create_validation_error,
)
from ..read_only import require_write_access
from ..runtime.action_constants import (
    _COMPONENT_RESPONSE_UNCONFIRMED_WARNING as _COMPONENT_RESPONSE_UNCONFIRMED_WARNING,
)
from ..runtime.action_constants import _NON_ENVELOPE_WARNING as _NON_ENVELOPE_WARNING
from ..runtime.action_constants import (
    _NON_STATE_CHANGING_DOMAINS as _NON_STATE_CHANGING_DOMAINS,
)
from ..runtime.action_constants import (
    _STATE_CHANGING_SERVICES as _STATE_CHANGING_SERVICES,
)
from ..runtime.action_params import (
    _parse_json_dict_param as _shared_parse_json_dict_param,
)
from ..runtime.actions import ServiceCallTools as SharedServiceCallTools
from ..runtime.event_tools import EventTools as _EventContractTools
from ..tool_api import ToolFailure
from ..utils.entity_membership import normalize_member_entity_ids
from .backup_access import guard_snapshot_route
from .bulk_selector import (
    _NON_AGGREGATE_ROOT_DOMAINS,
    BulkControlSelector,
    BulkSelectorInfrastructureError,
    BulkSelectorResolution,
    BulkSelectorValidationError,
    InfrastructureErrorCause,
    resolve_bulk_selector,
)
from .coercion import JSON_STRING_COERCION, parse_json_param
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints, write_hints
from .util_helpers import (
    _SERVICE_TO_STATE,
    BLOCKED_WS_WRITE_COMMANDS,
    is_single_entity_target,
)
from .ws_waiters import wait_for_state_change

# The ha_mcp_tools/call_service WS command: the first WRITE capability (Phase 3,
# issue #1813). When the component advertises ``call_service`` the consumer routes a
# single service call through this one in-process frame, which fires exactly one
# ``async_call`` and returns the REAL pre->post transition, replacing the legacy
# REST POST + hardcoded ``_SERVICE_TO_STATE`` guess + WS-subscribe verification.
# Named once so the routing helper and its tests agree on the wire string.
WS_CALL_SERVICE = "ha_mcp_tools/call_service"


class BulkControlOperation(TypedDict):
    """One entity action in a ha_bulk_control request."""

    entity_id: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact Home Assistant entity ID, e.g. 'light.kitchen'.",
        ),
    ]
    action: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Device action such as 'on', 'off', or 'toggle'. For lights, use "
                "'off' instead of the ha_call_service form 'turn_off'."
            ),
        ),
    ]
    parameters: NotRequired[
        Annotated[
            dict[str, Any],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional action parameters, e.g. {'brightness_pct': 30} "
                    "when action='on'. Each domain has a fixed allowlist of "
                    "supported keys; keys outside it are ignored rather than "
                    "rejected. Use ha_call_service for parameters this tool "
                    "does not carry."
                )
            ),
        ]
    ]
    timeout_seconds: NotRequired[
        Annotated[
            float,
            Field(
                ge=0,
                allow_inf_nan=False,
                # ``strict`` for the same reason validate_first carries it:
                # lax mode coerces ``true`` to 1.0, so a bool would land
                # downstream as a silent one-second timeout.
                strict=True,
                description=(
                    "Optional confirmation timeout. On the component path, all "
                    "operations share the maximum requested wait (default 10s, "
                    "capped at 60s); 0 disables confirmation waiting."
                ),
            ),
        ]
    ]
    validate_first: NotRequired[
        Annotated[
            bool,
            Field(
                strict=True,
                description=(
                    "Report an ENTITY_NOT_FOUND failure when the target entity "
                    "does not exist; default true. On the component batch path "
                    "this is detected from the captured pre-state rather than by "
                    "preventing dispatch. The action is always validated."
                ),
            ),
        ]
    ]


# Pydantic reads this runtime config when generating the TypedDict schema. Keep it
# outside the class body because mypy permits only field declarations there.
BulkControlOperation.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]
_BULK_CONTROL_OPERATION_ADAPTER = TypeAdapter(BulkControlOperation)


def _parse_bulk_operations(operations: Any) -> list[Any]:
    """Parse explicit bulk rows while preserving per-row runtime failures."""
    try:
        parsed_operations = parse_json_param(operations, "operations")
    except ValueError as exc:
        raise_tool_error(
            create_validation_error(
                f"Invalid operations parameter: {exc}",
                parameter="operations",
                invalid_json=True,
            )
        )
    if not isinstance(parsed_operations, list):
        raise_tool_error(
            create_validation_error(
                "Operations parameter must be a list",
                parameter="operations",
                details=f"Received type: {type(parsed_operations).__name__}",
            )
        )
    operations_list: list[Any] = []
    for index, operation in enumerate(parsed_operations):
        try:
            operations_list.append(
                _BULK_CONTROL_OPERATION_ADAPTER.validate_python(operation)
            )
        except ValidationError as exc:
            # include_input=False: a malformed row can carry a lock or alarm
            # code in its `parameters`, and str(exc) would otherwise write
            # that value to persistent logs.
            logger.warning(
                "ha_bulk_control operation %d failed schema validation: %s",
                index,
                exc.errors(include_url=False, include_input=False),
            )
            # The malformed row is deliberately preserved (not dropped) so
            # the runtime batch validator downstream still reports it as a
            # per-operation failure with its own diagnostics, instead of it
            # silently vanishing from the batch.
            operations_list.append(operation)
    return operations_list


def _expand_membership_transitively(
    entity_id: str, states_by_id: dict[str, Any], *, visited: set[str] | None = None
) -> set[str]:
    """Return every entity reachable from ``entity_id`` via nested
    group/aggregate membership -- direct members and, recursively, THEIR
    members too (an outer Zone containing an inner Room group, say).

    ``visited`` guards against a membership cycle; an entity already seen
    on this walk is treated as having no further members instead of
    recursing forever. Keyword-only and defaulted to ``None`` (allocated
    internally on first call) rather than a plain mutable positional
    parameter: today every top-level caller happens to pass a fresh
    ``set()``, but a positional mutable default is the kind of thing a
    later "optimization" hoists out of a loop -- which would silently
    start returning empty member sets (everything already "visited") and
    lose conflicts instead of erroring. Best-effort, not authoritative: an
    unknown or stateless entity simply contributes no members, mirroring
    ``_find_group_member_conflicts``'s own tolerance for a batch that
    references something ``states`` doesn't have -- this is a safety-net
    scan over the small set of entities in one batch, not the selector
    resolver's own graph-integrity validation (``bulk_selector._expand_entity``),
    which is right to raise on exactly those cases when it is actually
    resolving a dispatch set.
    """
    if visited is None:
        visited = set()
    if entity_id in visited:
        return set()
    visited.add(entity_id)
    state = states_by_id.get(entity_id)
    if state is None:
        return set()
    members = normalize_member_entity_ids(state.get("attributes"))
    if not members:
        return set()
    expanded = set(members)
    for member_id in members:
        expanded.update(
            _expand_membership_transitively(member_id, states_by_id, visited=visited)
        )
    return expanded


class _GroupConflict(NamedTuple):
    """One group/aggregate entity's conflict with an operations batch.

    ``unlisted_members``: this group's members that were NOT separately
    listed in the batch -- the entities the group's own cascade will
    change WITHOUT the caller having asked for them. This is the
    actionable half of the report: the caller needs to know which
    entities their attempt to exclude something actually failed to
    protect. Preferred over ``redundant_members`` whenever non-empty.
    ``redundant_members``: this group's members that WERE also listed
    separately -- harmless (each ends up in the requested state either
    way via the group's own fan-out), but still flagged as a conflict
    since a differing per-row action or parameters races that fan-out
    rather than safely overriding it. Reported only when
    ``unlisted_members`` is empty (every member is already accounted
    for), since naming the harmless rows instead of the harmed ones is
    the wrong report even when both are non-empty.
    """

    unlisted_members: list[str]
    redundant_members: list[str]


# Every listed member beyond this many is summarized as "+N more" rather
# than enumerated: `unlisted_members` is bounded by instance topology size
# (a group's total member count), not batch size, so a large group produces
# an unbounded error message otherwise. This is a fail-closed path -- the
# remedy sentence at the end is the entire value of the error -- and
# visibility/enforcement.py scans tool-error text for hidden entity IDs,
# refusing the whole call if one appears; enumerating every member makes
# that materially more likely for no benefit once the caller already has
# the point.
_MAX_LISTED_GROUP_MEMBERS = 10


def _format_member_list(members: list[str]) -> str:
    """Render a member list for an error message, capped at
    ``_MAX_LISTED_GROUP_MEMBERS`` (see its docstring)."""
    shown = ", ".join(members[:_MAX_LISTED_GROUP_MEMBERS])
    overflow = len(members) - _MAX_LISTED_GROUP_MEMBERS
    return f"{shown} (+{overflow} more)" if overflow > 0 else shown


def _find_group_member_conflicts(
    entity_ids: set[str], states: list[Any]
) -> dict[str, _GroupConflict]:
    """Return ``{group_entity_id: _GroupConflict}`` for every group/
    aggregate entity this batch targets alongside one or more of its own
    members -- direct or nested (a batch targeting an outer group and a
    leaf reachable only through an inner group it contains is exactly as
    unsafe as targeting the inner group and that leaf directly).

    Scenes ARE checked here, unlike ``bulk_selector``'s aggregate-root
    admission (``_NON_AGGREGATE_ROOT_DOMAINS``): the two ask different
    questions. Selector mode asks "may I expand this entity's ``entity_id``
    attribute to decide what to INCLUDE" -- no for a scene, since its
    configured targets can live anywhere in the house and would wrongly
    drag them into an area-scoped dispatch. This function asks "will
    dispatching this row also change something else named in this same
    batch" -- yes for a scene: ``scene.turn_on`` applies the scene's stored
    states to every entity it configures, exactly the same batch-internal
    race a real group's cascade creates. Skipping scenes here would leave
    that race open for the one entity type whose whole purpose is fanning
    a single action out to many others.
    """
    states_by_id = {
        state["entity_id"]: state
        for state in states
        if isinstance(state, dict) and isinstance(state.get("entity_id"), str)
    }
    conflicts: dict[str, _GroupConflict] = {}
    for entity_id in sorted(entity_ids):
        transitive_members = _expand_membership_transitively(entity_id, states_by_id)
        if not transitive_members:
            continue
        redundant_members = sorted((entity_ids & transitive_members) - {entity_id})
        if not redundant_members:
            continue
        unlisted_members = sorted(transitive_members - entity_ids)
        conflicts[entity_id] = _GroupConflict(unlisted_members, redundant_members)
    return conflicts


async def _reject_operations_group_member_conflicts(
    client: HomeAssistantClient, operations: list[Any]
) -> None:
    """Fail closed when an operations-mode batch targets a group/aggregate
    entity together with one or more of its own members.

    Confirmed live (2026-08-23): Home Assistant (Hue Room/Zone groups in
    particular) fans a service call on the GROUP entity out to every member
    regardless of what else is in the same batch -- an operations batch
    that turned off a Hue Room group plus 4 of its 5 members (deliberately
    omitting the 5th, to exclude it) still turned the 5th member off too,
    purely from the group row's own cascade. Listing a member row
    separately does not shield it, so this fails the WHOLE batch closed
    (nothing dispatched) rather than silently letting the group row's
    cascade override an exclusion operations mode has no way to express.

    Fails closed on its own states-fetch failure too: an unverifiable batch
    is not a verified-safe one, and this is the same infrastructure-failure
    stance ``bulk_selector.py`` takes for the analogous selector-mode read.
    ``client.get_states()`` itself now raises ``HomeAssistantConnectionError``
    rather than swallowing a malformed response to ``[]`` (see its
    docstring in ``rest_client.py``) -- that used to be the actual fail-OPEN
    hole here: an HA hiccup silently produced an empty states list, which
    read as "verified, no conflicts" instead of "could not verify".

    A single-entity (or empty/all-malformed) batch cannot contain a
    group/member conflict by construction, so the states fetch is skipped
    entirely for it. Every batch with 2+ distinct entities still pays one
    uncached, full ``GET /states`` here regardless of whether any of them
    turn out to be a group -- this trades that fixed per-call cost for
    simplicity over fetching only the batch's own (and transitively
    reachable) entities, which would need N concurrent per-entity reads
    instead of one bulk one and is bounded by batch size rather than
    instance size. Revisit if that cost becomes a real bottleneck.
    """
    entity_ids = {
        op["entity_id"]
        for op in operations
        if isinstance(op, dict) and isinstance(op.get("entity_id"), str)
    }
    if len(entity_ids) < 2:
        return
    try:
        states = await client.get_states()
    except ToolError:
        raise
    except Exception as exc:
        exception_to_structured_error(
            exc,
            context={"operation": "bulk operations group-safety check"},
            suggestions=[
                "Could not verify this operations batch against group/"
                + "aggregate membership, so nothing was dispatched.",
                "Retry the request; this may be a transient Home Assistant "
                + "connectivity issue.",
            ],
        )
        raise  # unreachable: exception_to_structured_error always raises
    conflicts = _find_group_member_conflicts(entity_ids, states)
    if not conflicts:
        return
    # Prefer naming the members that will be silently AFFECTED (not in the
    # batch, so nothing else in the call protects them) over the ones
    # merely redundant with the group's own row -- the redundant rows are
    # harmless and not what the caller needs to fix. Falls back to naming
    # the redundant ones only when every member is already accounted for
    # (no unlisted ones exist to report).
    detail = "; ".join(
        (
            f"'{group}' will also affect {_format_member_list(conflict.unlisted_members)}, "
            "which this batch did not list"
            if conflict.unlisted_members
            else f"'{group}' is redundant with its own already-listed "
            f"member(s) {_format_member_list(conflict.redundant_members)}"
        )
        for group, conflict in sorted(conflicts.items())
    )
    # A scene conflict is still real (see _find_group_member_conflicts'
    # docstring), but selector mode cannot express "act on most of a scene
    # while excluding some" -- scene is never an aggregate root there. That
    # remedy sentence is only worth offering when at least one conflict is
    # a real, selector-expressible aggregate.
    non_scene_conflict = any(
        not any(group.startswith(f"{d}.") for d in _NON_AGGREGATE_ROOT_DOMAINS)
        for group in conflicts
    )
    selector_remedy = (
        " To act on most of a group while excluding specific members, use "
        "selector mode instead: exclude_entity_ids goes INSIDE selector, not "
        "as a top-level argument, e.g. "
        '{"selector": {"domain": "light", "area_ids": ["<area_id>"], '
        '"exclude_entity_ids": ["<entity_to_skip>"]}, "action": "off"}. '
        "area_ids/floor_ids must be exact registry IDs (call "
        "ha_list_floors_areas to look them up), not display names."
        if non_scene_conflict
        else ""
    )
    raise_tool_error(
        create_validation_error(
            "This operations batch targets a group/aggregate entity "
            f"together with one or more of its own individual members: {detail}. "
            "Home Assistant applies the action to every member when the "
            "group entity is targeted, regardless of what else is listed "
            "separately -- a member row does not protect or exempt it from "
            "the group's own action, and one absent from the batch entirely "
            "is not excluded by that absence either. Target ONLY the group, "
            "or ONLY the specific member(s) you want affected, never both "
            f"in the same call.{selector_remedy}",
            parameter="operations",
        )
    )


def _selector_only_parameter_offender(
    *,
    action: str | None,
    parameters: dict[str, Any] | None,
    timeout_seconds: float | None,
    validate_first: bool,
    dry_run: bool,
) -> str | None:
    """Name the one selector-only parameter set on an operations-mode call.

    Five distinct mistakes (action, parameters, timeout_seconds,
    validate_first, dry_run) used to share one message that always blamed
    "selector" — the one parameter that was demonstrably absent, since this
    is only reached when ``selector is None``. Naming the actual offender
    lets the caller fix the real mistake on the first try.
    """
    for name, value in (
        ("action", action),
        ("parameters", parameters),
        ("timeout_seconds", timeout_seconds),
    ):
        if value is not None:
            return name
    if validate_first is not True:
        return "validate_first"
    if dry_run:
        return "dry_run"
    return None


# action, parameters, timeout_seconds and validate_first are all declared
# fields on BulkControlOperation (see its class docstring) -- in operations
# mode they belong on each row, not as a top-level tool argument, so the
# caller's actual fix is to move the value, not delete it. Only dry_run has
# no per-row equivalent (there is no such thing as "dry-run this one row"),
# so it alone gets the remove-or-switch-modes remedy.
_PER_ROW_SELECTOR_ONLY_PARAMETERS = frozenset(
    {"action", "parameters", "timeout_seconds", "validate_first"}
)

# One representative, correctly-typed sample value per per-row parameter,
# for the worked example below. A bare "..." placeholder rendered inside a
# Python dict literal is always a quoted STRING regardless of the field's
# real type -- for timeout_seconds (a float) that example teaches the
# model the wrong shape for the exact value it is being told to copy.
_PER_ROW_PARAMETER_EXAMPLE_VALUES: dict[str, Any] = {
    "action": "on",
    "parameters": {"brightness_pct": 30},
    "timeout_seconds": 5,
    "validate_first": True,
}


def _selector_only_parameter_message(offending_parameter: str) -> str:
    """Build the remedy for one selector-only parameter used in operations mode.

    Telling the caller to remove the parameter (or abandon operations mode
    entirely) discards their intent for the four fields that DO have a
    per-row home: ``operations=[...], parameters={...}`` almost certainly
    meant "apply these parameters to my operations", and the fix is to
    move the value into each row, not delete it.
    """
    if offending_parameter in _PER_ROW_SELECTOR_ONLY_PARAMETERS:
        # Built from a dict, not a hardcoded literal: 'action' is itself one
        # of the four per-row parameters this branch handles, and a fixed
        # "..., 'action': 'on', 'action': ...}" literal would render a
        # duplicate-key example -- shown to a model that is being told to
        # copy it -- whenever action is the actual offender.
        example_row: dict[str, Any] = {"entity_id": "light.kitchen"}
        if offending_parameter != "action":
            example_row["action"] = "on"
        example_row[offending_parameter] = _PER_ROW_PARAMETER_EXAMPLE_VALUES[
            offending_parameter
        ]
        return (
            f"'{offending_parameter}' is a per-operation field in operations "
            f"mode (see BulkControlOperation), not a top-level tool argument "
            f"-- move it onto each row instead, e.g. {example_row}, "
            f"and remove the top-level '{offending_parameter}' argument."
        )
    return (
        f"'{offending_parameter}' is a selector-only parameter and cannot be "
        "used with operations. Either remove "
        f"'{offending_parameter}' from this call, or switch to selector mode "
        "by replacing 'operations' with 'selector' (+ 'action')."
    )


# Per-cause (message, suggestions) for BulkSelectorInfrastructureError (see
# its docstring in bulk_selector.py). Not every infrastructure failure is a
# network problem -- CONNECTION_FAILED's own DEFAULT_SUGGESTIONS
# (check HA is running / verify HOMEASSISTANT_URL / check network) are
# actively unhelpful for a malformed local registry row or a corrupt local
# visibility config file, so those causes get their own actionable text
# instead of inheriting the connectivity defaults.
_INFRASTRUCTURE_ERROR_SUGGESTIONS: dict[
    InfrastructureErrorCause, tuple[str, list[str]]
] = {
    InfrastructureErrorCause.CONNECTIVITY: (
        "Could not resolve the selector because Home Assistant data was unavailable",
        [
            "Check if Home Assistant is running and accessible",
            "Retry the request; this may be a transient registry read failure",
        ],
    ),
    InfrastructureErrorCause.MALFORMED_DEVICE_REGISTRY: (
        "Could not resolve the selector because Home Assistant's device "
        "registry returned a malformed entry",
        [
            "This indicates malformed data in Home Assistant's own device "
            + "registry, not a problem with the selector",
            "Check Home Assistant's logs for device-registry errors, or "
            + "restart Home Assistant if the issue persists",
        ],
    ),
    InfrastructureErrorCause.VISIBILITY_CONFIG: (
        "Could not resolve the selector because the entity visibility "
        "filter could not be evaluated safely",
        [
            "Check the Entity Visibility tab in the ha-mcp settings UI -- "
            + "entity_visibility.json may be corrupt or invalid",
            "If the config looks fine, this may be a temporary Home "
            + "Assistant registry issue -- check Home Assistant is running "
            + "and retry",
        ],
    ),
}


def _attach_resolution_to_response(
    response: dict[str, Any], resolution: BulkSelectorResolution
) -> None:
    """Attach ``resolution.summary()`` to a response, surfacing its warnings
    at the top level.

    Per .gemini/styleguide.md "Tool Tags and Return Values", ``warnings``
    is always a top-level ``list[str]``, never nested inside another field.
    ``resolution.summary()`` nests its own warnings (e.g. "N entities were
    hidden") under ``resolution`` for internal cohesion, so this pops them
    back out. ``response`` may already carry dispatch-time warnings (e.g.
    from ``bulk_device_control``) -- those are extended, not overwritten.
    """
    summary = resolution.summary()
    resolution_warnings = summary.pop("warnings", [])
    response["resolution"] = summary
    if resolution_warnings:
        response.setdefault("warnings", []).extend(resolution_warnings)


class _AmbiguousDispatch:
    """Sentinel type for a post-send-ambiguous component write (see below)."""


# Returned by ``_call_service_via_component`` when the component frame was SENT but
# its response/confirmation never arrived (a response-wait timeout or a post-send
# transport drop): the write MAY have landed, so the caller reports it as ``partial``
# and MUST NOT re-POST via the legacy path (D9 at-most-once — an ambiguous post-send
# outcome is never retried). Distinct from ``None`` (the component provably never
# dispatched → a safe legacy first fire).
_COMPONENT_DISPATCH_AMBIGUOUS = _AmbiguousDispatch()


def _parse_json_dict_param(data: Any, *, type_error_message: str) -> Any:
    try:
        return _shared_parse_json_dict_param(
            data, type_error_message=type_error_message
        )
    except ToolFailure as exc:
        raise_tool_error(exc.data)


logger = logging.getLogger(__name__)

# Services that produce observable state changes on entities

# Domains where a service call does not move the TARGET entity's own primary
# state to a value the verifier can wait for. ``scene`` belongs here with
# ``automation``/``script``: activating a scene changes the member entities,
# but the scene entity's own state is a last-activated timestamp that never
# becomes "on"/"off" — so waiting for ``turn_on`` -> "on" always times out and
# only appends a spurious "could not be verified" warning (~10s wasted).

#: Emitted when a ``return_response=true`` reply is not HA's
#: ``{"changed_states": [...], "service_response": ...}`` envelope. Without it an
#: empty ``result`` reads as an affirmative "no entity states changed" rather than
#: "the records could not be separated out". Module-level so the tests that
#: pin the behaviour assert against this exact string instead of a prose fragment
#: that goes tautologically green when the wording is edited.

#: Emitted when the component served a ``return_response`` call but returned no
#: ``service_response`` key. The component only sets that key for a non-null
#: response, so an absent key is ambiguous server-side: the service may genuinely
#: have returned nothing, or the component may have discarded the response on its
#: dispatched-but-unconfirmed path. Only raised alongside ``partial`` — the case
#: where the discard is actually possible — so a plain null response stays quiet.

# ``_SERVICE_TO_STATE`` (the service -> expected primary-state map) is the single
# source of truth in ``util_helpers`` — imported above and shared with the bulk
# consumer (``device_control``) so both write paths hand the component the same
# confirmation hint.


# WebSocket commands that stream events or reply in two phases (an initial ack
# then the real payload as a follow-up event) rather than resolving to a single
# terminal result. ha_call_service's ws_command escape hatch only supports
# one-shot request/response commands, so these are rejected: the "subscribe"
# substring catches subscription commands, and the set names known two-phase /
# streaming commands whose names don't contain "subscribe". The set is a floor,
# not an exhaustive list -- HA's WS command naming isn't consistent enough for a
# name check alone to be fully reliable.
_WS_COMMAND_EVENT_BLOCKLIST = frozenset(
    {
        "render_template",  # event-based; use ha_eval_template instead
        "system_health/info",  # two-phase (see tools_system._fetch_health_info)
        "assist_pipeline/run",  # streams pipeline events
    }
)

# Substrings that mark a WS command as streaming / subscription-based even when
# its name isn't in the blocklist above. Such commands ack once and then push
# follow-up events on the same id; the one-shot send_command path returns the
# ack and leaks the subscription. "subscribe" covers subscribe_* and */subscribe;
# "stream" covers history/stream, logbook/event_stream, camera/stream, ...;
# "start_preview" covers template/start_preview and the config-flow preview family.
_WS_STREAMING_SUBSTRINGS = ("subscribe", "stream", "start_preview")

# One-shot WS commands that re-enter Home Assistant's service invocation. Routing
# them through the escape hatch would bypass the service-mode guards (notably the
# reserved ha_mcp_tools domain block), so they are rejected -- use the
# domain/service parameters for service calls instead.
_WS_COMMAND_SERVICE_INVOKERS = frozenset({"call_service", "execute_script"})

# Reserved WebSocket envelope keys the transport owns. Allowing them inside data
# would let a caller override the validated command type (defeating every check
# below) or collide with the transport's message id, so they are rejected.
_WS_RESERVED_ENVELOPE_KEYS = frozenset({"type", "id"})


def _is_streaming_ws_command(command_type: str) -> bool:
    """Return True for subscription / streaming / two-phase WS commands."""
    lowered = command_type.lower()
    if lowered in _WS_COMMAND_EVENT_BLOCKLIST:
        return True
    return any(sub in lowered for sub in _WS_STREAMING_SUBSTRINGS)


def _build_service_suggestions(
    domain: str, service: str, entity_id: str | None
) -> list[str]:
    """Build common error suggestions for service call failures."""
    return [
        f"Verify {entity_id} exists using ha_get_state()"
        if entity_id
        else "Specify an entity_id for targeted service calls",
        f"Check available services for {domain} domain using ha_get_skill_guide",
        "Use ha_search() to find correct entity IDs",
    ]


class ServiceTools(SharedServiceCallTools):
    """Service call and device operation tools for Home Assistant."""

    def __init__(self, client: HomeAssistantClient, device_tools: Any) -> None:
        self._client = client
        self._device_tools = device_tools

    _parse_service_data = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._parse_service_data)
    )
    _validate_service_call_params = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._validate_service_call_params)
    )
    _reject_incompatible_ws_params = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._reject_incompatible_ws_params)
    )
    _parse_result_projection_params = staticmethod(
        legacy_errors_sync(SharedServiceCallTools._parse_result_projection_params)
    )
    _raise_for_missing_or_unavailable_target = staticmethod(
        legacy_errors_sync(
            SharedServiceCallTools._raise_for_missing_or_unavailable_target
        )
    )
    _build_component_call_response = legacy_errors_sync(
        SharedServiceCallTools._build_component_call_response
    )

    async def _validate_entity_before_wait(self, entity_id: str | None) -> str | None:
        """Fetch entity_id's pre-call state, failing fast on a settled bad target.

        ``entity_id`` stays optional in the signature to match the caller's
        own ``str | None`` (a service call may target zero entities); the
        one current call site only reaches this when ``should_wait`` (which
        embeds an ``entity_id is not None`` check among several AND-ed
        conditions) is true, so ``entity_id`` is always real there in
        practice -- but that invariant lives in a boolean a few lines away,
        not in a form the type checker can see through. Narrowing here
        instead of trusting the caller keeps ``get_entity_state`` (which
        genuinely requires a ``str``) honestly typed.

        A clean 404 (the entity does not exist) settles the question with
        certainty before any service dispatch or 10s state-change wait, so
        raise immediately instead of silently degrading into a wait that can
        never confirm. Any other fetch failure is NOT conclusive -- it
        returns ``None`` and lets the call proceed as before, since the
        entity may well exist and this could be a transient blip.

        Deliberately NOT fast-failed here: a fetched ``"unavailable"``
        state. Unlike a 404, it does not prove the target can never respond
        -- dispatching the service is exactly what can reconnect it (e.g. a
        device that wakes on the very command being sent), and failing here
        would mean that command never reaches Home Assistant at all. The
        entity's initial state is still returned so ``_verify_state_change``
        can classify a genuine confirmation lapse as ENTITY_UNAVAILABLE only
        if a live re-check shows it is STILL unavailable afterward -- mirrors
        the component path's post-dispatch-state check.

        A comma-separated ``entity_id`` ("light.a,light.b") or a Home
        Assistant magic broadcast target (``entity_id="all"`` /
        ``ENTITY_MATCH_ALL``, or ``"none"``) is a valid target for the
        service-call payload, but the single-entity ``/api/states/<id>``
        endpoint has no way to resolve either -- a 404 there proves nothing
        about whether the underlying call can succeed, so the fast-fail is
        skipped entirely for one (see ``is_single_entity_target``; matches
        the component route's own comma exclusion in
        ``_maybe_component_call_service``).
        """
        if entity_id is None:
            return None
        is_single_target = is_single_entity_target(entity_id)
        try:
            state_data = await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if is_single_target and e.status_code == 404:
                raise_tool_error(create_entity_not_found_error(entity_id))
            logger.debug(
                f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded"
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.debug(
                f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded"
            )
            return None

        return state_data.get("state") if state_data else None

    async def _verify_state_change(
        self,
        entity_id: str,
        service: str,
        initial_state: str | None,
        response: dict[str, Any],
    ) -> None:
        """Wait for and verify entity state change after a service call, updating response in place.

        An entity that was ``"unavailable"`` at dispatch time was NOT failed
        fast (see ``_validate_entity_before_wait``) -- it may have reconnected
        once the command actually reached it. Only once the wait genuinely
        lapses does a fresh live re-check decide whether it's still
        unavailable (report the precise ``ENTITY_UNAVAILABLE``) or reconnected
        to some state other than the hint (leave the existing generic
        partial/timeout wording) -- mirrors the component path's
        post-dispatch-state check.
        """
        try:
            expected = _SERVICE_TO_STATE.get(service)
            new_state = await wait_for_state_change(
                self._client,
                entity_id,
                expected_state=expected,
                initial_state=initial_state,
                timeout=10.0,
            )
            if new_state:
                response["verified_state"] = new_state.get("state")
                return
            if initial_state == "unavailable" and await self._still_unavailable(
                entity_id
            ):
                raise_tool_error(
                    create_entity_unavailable_after_dispatch_error(entity_id)
                )
            response.setdefault("warnings", []).append(
                "Service executed but state change could not be verified within timeout."
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            response.setdefault("warnings", []).append(
                f"Service executed but state verification failed: {e}"
            )

    async def _still_unavailable(self, entity_id: str) -> bool:
        """Live re-check: True only when a successful fetch explicitly shows
        ``"unavailable"``.

        A 404 here means the entity was REMOVED since the initial fetch — a
        more specific outcome than "still unavailable", so it raises
        ENTITY_NOT_FOUND directly rather than being folded into it. Any other
        fetch failure (network blip, an expired token surfacing as
        ``HomeAssistantAuthError`` -- a sibling of ``HomeAssistantAPIError``,
        not a subclass, so it lands in the generic branch below) or a
        response with no usable ``state`` is inconclusive, not proof either
        way, so it returns ``False`` and lets the caller fall through to the
        generic verification-failed wording instead of confidently asserting
        an unproven "still unavailable". Logged at WARNING (matching the
        sibling re-check in ``_validate_entity_before_wait`` and
        ``wait_for_state_change`` in ws_waiters.py) so an operational
        failure here -- an expired token, in particular -- leaves a
        server-side trace instead of silently reading as a generic timeout.
        """
        try:
            current = await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                raise_tool_error(create_entity_not_found_error(entity_id))
            logger.warning(
                f"Post-timeout unavailable re-check for {entity_id} failed: {e} — treating as inconclusive"
            )
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Post-timeout unavailable re-check for {entity_id} failed: {e} — treating as inconclusive"
            )
            return False
        return isinstance(current, dict) and current.get("state") == "unavailable"

    def _handle_connection_error(
        self,
        error: HomeAssistantConnectionError,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        return_response: bool = False,
    ) -> dict[str, Any]:
        """Handle a HomeAssistantConnectionError raised while calling a service.

        Timeouts are treated as partial success (the service was dispatched but
        Home Assistant did not respond in time) and return a partial-success
        response. Non-timeout connection errors raise a structured ToolError.
        """
        # Check if this is a timeout - for service calls, timeouts typically
        # mean the service was dispatched but HA didn't respond in time.
        # The operation is likely still running (e.g., update.install, long automations).
        if isinstance(error.__cause__, httpx.TimeoutException):
            return self._build_timeout_response(
                domain, service, entity_id, data, return_response=return_response
            )
        # Non-timeout connection errors are real failures
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=_build_service_suggestions(domain, service, entity_id),
        )
        return None  # unreachable: exception_to_structured_error always raises

    @staticmethod
    def _raise_unexpected_call_service_error(
        error: Exception,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
    ) -> NoReturn:
        """Raise a structured ToolError for an unexpected ha_call_service failure."""
        suggestions = _build_service_suggestions(domain, service, entity_id)
        if entity_id:
            suggestions.append(
                f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')"
            )
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=suggestions,
        )

    async def _send_ws_command_mapped(
        self, command_type: str, command_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send an arbitrary WS command, mapping a dead transport to ToolError.

        The ws_command branch returns before ``ha_call_service``'s own try
        block, so without this a transport failure raised by
        ``send_websocket_message`` (#1947) would escape the tool unstructured.
        """
        try:
            result: dict[str, Any] = await self._client.send_websocket_message(
                {"type": command_type, **command_params}
            )
            return result
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"ws_command": command_type},
                suggestions=[
                    "Check the Home Assistant connection",
                    "Retry once the WebSocket link is back",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _call_ws_command(
        self,
        ws_command: str,
        data: str | dict[str, Any] | None,
        *,
        domain: str | None,
        service: str | None,
    ) -> dict[str, Any]:
        """Send a one-shot WebSocket command via ha_call_service's escape hatch.

        Reaches Home Assistant WebSocket commands that are not registered
        services. Only one-shot request/response commands are supported —
        streaming / subscription commands are rejected up front.
        """
        command_type = ws_command.strip()
        if domain is not None or service is not None:
            raise_tool_error(
                create_validation_error(
                    "Provide either domain + service (a registered service call) "
                    "OR ws_command (a raw WebSocket command), not both.",
                    parameter="ws_command",
                )
            )
        if not command_type:
            raise_tool_error(
                create_validation_error(
                    "ws_command must be a non-empty WebSocket command type "
                    "(the message's 'type' field).",
                    parameter="ws_command",
                )
            )
        if _is_streaming_ws_command(command_type):
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' is a streaming or two-phase "
                    "command; ha_call_service only sends one-shot "
                    "request/response commands. For template rendering use "
                    "ha_eval_template.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in _WS_COMMAND_SERVICE_INVOKERS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' invokes Home Assistant services "
                    "and would bypass ha_call_service's safeguards. Use the "
                    "domain/service parameters for service calls instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower().startswith("ha_mcp_tools/"):
            raise_tool_error(
                create_validation_error(
                    "ha_call_service cannot invoke 'ha_mcp_tools/*' WebSocket "
                    "commands. Use the dedicated ha-mcp tools instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in BLOCKED_WS_WRITE_COMMANDS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' mutates persistent state that a "
                    "dedicated tool guards with backups and conflict checks. Use "
                    "the corresponding ha-mcp tool (e.g. ha_config_set_dashboard, "
                    "ha_set_area_or_floor, ha_remove_entity) instead.",
                    parameter="ws_command",
                )
            )
        command_params = (
            _parse_json_dict_param(
                data, type_error_message="ws_command data must be a JSON object"
            )
            or {}
        )
        reserved = _WS_RESERVED_ENVELOPE_KEYS & command_params.keys()
        if reserved:
            raise_tool_error(
                create_validation_error(
                    "data must not contain the reserved WebSocket envelope key(s) "
                    f"{', '.join(sorted(reserved))}; the command type is set by "
                    "ws_command, and the message id is managed by the transport.",
                    parameter="data",
                )
            )
        # send_websocket_message returns a {"success": ...} dict for anything
        # HA answered with, which the result-shape check below turns into a
        # structured error. A dead transport raises instead (#1947), and this
        # branch runs BEFORE ha_call_service's own try block, so the mapping
        # has to happen here or the exception escapes the tool unstructured.
        guard_snapshot_route(ws_command=command_type, ws_params=command_params)
        result = await self._send_ws_command_mapped(command_type, command_params)

        if not isinstance(result, dict) or not result.get("success", False):
            error_msg = (
                result.get("error") if isinstance(result, dict) else None
            ) or "WebSocket command failed"
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_msg),
                    context={"ws_command": command_type},
                    suggestions=[
                        "Verify the command type and the parameters it requires",
                        "Confirm the target still exists",
                    ],
                )
            )

        return {
            "success": True,
            "ws_command": command_type,
            "parameters": command_params or None,
            "result": result.get("result"),
            "message": f"Successfully executed WebSocket command '{command_type}'",
        }

    async def _call_service_via_component(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_ids: list[str],
        wait: bool,
        timeout: float,
        return_response: bool,
    ) -> dict[str, Any] | _AmbiguousDispatch | None:
        """Send once through the component and distinguish not-sent from unknown.

        None permits the first legacy dispatch only after a capability miss,
        connection establishment failure, explicit CommandNotSent, or the
        specific unknown_command response. A generic command error is ambiguous:
        a Core handler can mutate before raising. Timeouts, socket failures and
        malformed success frames are likewise never replayed.

        The native backend calls the same prep/do owner directly and has no
        transport fallback. Both paths retain the resolved-domain guard and
        expected-state confirmation hint.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "call_service"):
            return None
        # PRE-SEND: an establishment failure means the frame provably never reached
        # the component. Split into its own try so a POST-SEND failure below is never
        # misclassified as pre-send → a safe legacy first fire.
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s establishment failed; falling back to legacy: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return None
        # ``send_command`` transmits the frame INSIDE itself, AFTER its readiness guard
        # and the actual socket write — so exception TYPE marks the send boundary:
        # ``HomeAssistantCommandNotSent`` is raised ONLY at the readiness guard (the one
        # provably-never-sent site → safe legacy first fire); only unknown_command proves that no owner ran; a response-wait TIMEOUT, a send() that raised (bytes may
        # already be on the socket), or a post-send transport drop (a mid-await socket
        # close raises plain ``HomeAssistantConnectionError``) is POST-SEND/AMBIGUOUS →
        # partial, never retried.
        # The confirmation HINT: the expected primary state after ``service`` (or
        # ``None`` for a service with no known primary state). The component confirms
        # only on REACHING this state — skipping a multi-phase service's intermediate
        # states / attribute-only noise — and immediate-matches an idempotent no-op; a
        # ``None`` hint keeps its any-first-event confirmation. It governs confirmation
        # TIMING only; the component still returns the REAL observed transition.
        expected_state = _SERVICE_TO_STATE.get(service)
        try:
            raw = await ws.send_command(
                WS_CALL_SERVICE,
                domain=domain,
                service=service,
                service_data=service_data,
                entity_ids=entity_ids,
                wait=wait,
                timeout=timeout,
                return_response=return_response,
                expected_state=expected_state,
            )
        except HomeAssistantCommandNotSent as exc:
            # PRE-SEND: the frame provably never left the process (the send_command
            # readiness guard — the one never-sent site). The write never happened, so
            # legacy REST is a safe first fire.
            logger.warning(
                "%s not sent; falling back to legacy: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return None
        except HomeAssistantCommandError as exc:
            # Only unknown_command proves that this owner never ran. Core can
            # mutate and then raise any other error: do not replay that write.
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            logger.warning(
                "%s command error; reporting unknown outcome without retry: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return _COMPONENT_DISPATCH_AMBIGUOUS
        except Exception as exc:  # noqa: BLE001
            # HomeAssistantCommandTimeout (response-wait expired — the frame WAS sent)
            # or any post-send transport drop (e.g. a pooled-WS drop after send). The
            # component may still be lawfully mid-write, so this is ambiguous-
            # dispatched: report partial, NEVER re-POST (D9 at-most-once).
            logger.warning(
                "%s post-send timeout/drop; reporting partial (not retried): %r",
                WS_CALL_SERVICE,
                exc,
            )
            return _COMPONENT_DISPATCH_AMBIGUOUS
        result = raw.get("result")
        # A SUCCESS result frame is produced ONLY after the prep ran to completion (the
        # single async_call fired), so a malformed/unusable success envelope means the
        # write already HAPPENED. Report it AMBIGUOUS (partial, never re-POSTed) — a
        # ``None`` here would route to the legacy REST path and DOUBLE-APPLY. The
        # happy-path envelope is a dict whose ``dispatched`` is True: presence of the
        # key is not enough, since a received post-dispatch envelope whose
        # ``dispatched`` isn't True is one we cannot trust to re-fire.
        if not isinstance(result, dict) or result.get("dispatched") is not True:
            return _COMPONENT_DISPATCH_AMBIGUOUS
        return result

    async def _maybe_component_call_service(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any] | None:
        """Route a confirmable single call through the component; ``None`` → do legacy.

        The component route is taken ONLY when confirming a single entity
        (``should_wait``). The capability's entire value is the real confirmed
        pre->post transition; for a non-confirmed call (``wait=False``, no / multi
        entity, or a non-state-changing domain) the component returns
        ``transitions=[]`` → ``result:[]``, silently dropping the changed-states body
        the legacy REST POST returns (e.g. a scene's member states). For those the
        legacy single POST costs the same one round-trip and is strictly richer, so
        this returns ``None`` and the caller stays on legacy.

        Returns a FINAL ``ha_call_service`` response when the component served the
        call: the mapped transition, or — on a post-send timeout / transport drop
        (the ambiguous sentinel) — the same dispatched-but-unconfirmed ``partial`` the
        legacy timeout path builds, NEVER re-POSTed (D9 at-most-once). Returns ``None``
        when the component was not used or provably never dispatched, so the caller
        runs the legacy REST path as a safe first fire.

        ``verbose`` routes to legacy too: it promises the FULL propagation chain
        (every downstream changed state), which the component path cannot deliver — it
        returns only the confirmation targets' ``new_state``s — so the richer legacy
        POST serves it instead.
        """
        # A comma-separated entity_id ("light.a,light.b") is a valid multi-target the
        # compaction path expands, but the component confirms one LITERAL entity_id: it
        # would wait for the nonexistent literal "light.a,light.b" and report a false
        # ``partial`` with an empty result even though HA changed both real entities.
        # Treat a comma as the multi-target signal → legacy REST POST (parity with the
        # verbose / non-confirmed early-outs above, which the component cannot serve).
        if not should_wait or verbose or (entity_id and "," in entity_id):
            return None
        component_result = await self._call_service_via_component(
            domain=domain,
            service=service,
            service_data=service_data,
            entity_ids=[entity_id] if entity_id else [],
            wait=True,
            timeout=10.0,
            return_response=return_response,
        )
        if isinstance(component_result, _AmbiguousDispatch):
            return self._build_timeout_response(
                domain, service, entity_id, data, return_response=return_response
            )
        if component_result is None:
            return None
        return self._build_component_call_response(
            component_result,
            domain=domain,
            service=service,
            entity_id=entity_id,
            data=data,
            should_wait=should_wait,
            return_response=return_response,
            verbose=verbose,
            fields=fields,
            attribute_keys=attribute_keys,
        )

    ha_call_service = tool(
        name="ha_call_service",
        tags={"Service & Device Control"},
        annotations=write_hints(
            "Call Service", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(SharedServiceCallTools.ha_call_service)))

    def _require_write_access(self) -> None:
        require_write_access("ha_call_service")

    async def _check_service_policy(self, domain: str, service: str) -> None:
        guard_snapshot_route(domain=domain, service=service)

    async def _call_raw_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        *,
        return_response: bool,
    ) -> Any:
        return await self._client.call_service(
            domain, service, service_data, return_response=return_response
        )

    def _map_service_error(self, error: Exception, **kwargs: Any) -> Any:
        if isinstance(error, HomeAssistantConnectionError):
            return self._handle_connection_error(error, **kwargs)
        if isinstance(error, (ToolError, ToolFailure)):
            raise error
        self._raise_unexpected_call_service_error(
            error,
            domain=kwargs["domain"],
            service=kwargs["service"],
            entity_id=kwargs["entity_id"],
        )

    @tool(
        name="ha_get_operation_status",
        tags={"Service & Device Control"},
        annotations=read_only_hints("Get Operation Status", open_world=False),
    )
    @log_tool_usage
    async def ha_get_operation_status(
        self,
        operation_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Single operation ID, or a list of IDs for a bulk status check."
                ),
            ),
        ],
        timeout_seconds: Annotated[
            float,
            Field(
                ge=0,
                allow_inf_nan=False,
                description=(
                    "Seconds to wait for a pending operation to finish before "
                    "returning its status. 0 returns the current status at once."
                ),
            ),
            # Home Assistant's MCP client abandons any call after 10 seconds.
        ] = 8,
    ) -> dict[str, Any]:
        """
        Get the status of one or more device operations with real-time WebSocket verification.

        Bulk checks poll all operations concurrently under one shared
        timeout_seconds window and report per-item failures inside
        detailed_results instead of aborting the batch.

        Use this to track the operation_ids that ha_bulk_control returns.
        For current entity states, use ha_get_state instead.
        """
        try:
            # JSON_STRING_COERCION turns a '["op1","op2"]' string into a list
            # before the body runs, so operation_id is already the final shape.
            if isinstance(operation_id, list):
                result = await self._device_tools.get_bulk_operation_status(
                    operation_ids=operation_id, timeout_seconds=timeout_seconds
                )
                return cast(dict[str, Any], result)
            result = await self._device_tools.get_device_operation_status(
                operation_id=operation_id, timeout_seconds=timeout_seconds
            )
            return cast(dict[str, Any], result)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            op_context: dict[str, Any] = {"operation_id": operation_id}
            exception_to_structured_error(
                e,
                context=op_context,
                suggestions=[
                    "Verify the operation ID(s) are valid",
                    "Use ha_get_state() to check current entity states instead",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_bulk_control",
        tags={"Service & Device Control"},
        annotations=write_hints(
            "Bulk Control", destructive=True, idempotent=False, open_world=False
        ),
    )
    @log_tool_usage
    async def ha_bulk_control(
        self,
        operations: Annotated[
            list[SkipValidation[BulkControlOperation]],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Explicit entity operations. "
                    "Each item requires exact entity_id and action. Use "
                    "action='off', not service='turn_off'."
                )
            ),
            # Declared type stays `list[...]` (not `| None`) so the generated
            # tool schema shows `operations` as always a list; `cast(Any, None)`
            # supplies the runtime default without a `| None` mypy would then
            # require type-narrowing for at every use below `selector is None`.
        ] = cast(Any, None),
        parallel: Annotated[
            bool,
            Field(
                description=(
                    "Dispatch operations concurrently (default) or one at a "
                    "time when False."
                ),
            ),
        ] = True,
        ctx: Context | None = None,
        selector: Annotated[
            SkipValidation[BulkControlSelector] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional exact structural scope using domain plus area_ids "
                    "and/or floor_ids, with optional exclude_entity_ids."
                )
            ),
        ] = None,
        action: Annotated[
            str | None,
            Field(description="One device action applied to every resolved leaf."),
        ] = None,
        parameters: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(description="Optional action parameters for selector mode."),
        ] = None,
        timeout_seconds: Annotated[
            float | None,
            Field(
                ge=0,
                le=60,
                allow_inf_nan=False,
                strict=True,
                description=(
                    "Selector mode only: confirmation wait in seconds for every "
                    "resolved entity. In operations mode set timeout_seconds on "
                    "each operation instead; a top-level value is rejected in "
                    "operations mode."
                ),
            ),
        ] = None,
        validate_first: Annotated[
            bool,
            Field(
                strict=True,
                description=(
                    "Selector mode only: report ENTITY_NOT_FOUND for a target "
                    "that does not exist. In operations mode set "
                    "validate_first on each operation instead; a top-level "
                    "False is rejected in operations mode."
                ),
            ),
        ] = True,
        dry_run: Annotated[
            bool,
            Field(
                strict=True,
                description="Selector mode only; True is rejected in operations mode.",
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage explicit operations or one deterministic structural bulk action.

        When NOT to use: use ``ha_call_service`` for service-specific payloads or
        backend-native group targeting, and ``ha_search`` for fuzzy name discovery.

        **Operations mode** (``operations``, no ``selector``): put every target in
        this one call. Parallel execution is the default, and invalid items are
        reported without aborting valid operations in the same batch — but a batch
        in which every item fails validation dispatches nothing and fails the call.
        A batch that targets a group/aggregate entity together with one or more of
        its own individual members also fails closed (nothing dispatched): Home
        Assistant applies the action to every member when the group is targeted
        regardless of what else is listed, so a member row cannot exclude that
        member from the group's own action. Use selector mode with
        ``exclude_entity_ids`` when a group action must exclude specific members.

        **Selector mode** (``selector`` + ``action``): use exact area or floor IDs
        when exclusions must be applied after recursively expanding generic
        aggregate membership. Resolves a frozen visible leaf set before dispatch;
        it is not transactional, so Home Assistant may still report per-leaf
        failures. A selector resolving to more than 100 entities fails closed
        instead of dispatching a partial/oversized batch — narrow it (a more
        specific area/floor, or add ``exclude_entity_ids``) and retry. Set
        ``dry_run`` to preview the resolved set without changing state.
        """
        if operations is None and selector is None:
            raise_tool_error(
                create_validation_error(
                    "Provide exactly one of operations or selector; neither was given",
                    parameter="operations",
                )
            )
        if operations is not None and selector is not None:
            raise_tool_error(
                create_validation_error(
                    "Provide exactly one of operations or selector; both were given",
                    parameter="selector",
                )
            )

        if selector is not None:
            return await self._run_bulk_selector(
                cast(dict[str, Any], selector),
                action=action,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                validate_first=validate_first,
                dry_run=dry_run,
                parallel=parallel,
                ctx=ctx,
            )

        offending_parameter = _selector_only_parameter_offender(
            action=action,
            parameters=parameters,
            timeout_seconds=timeout_seconds,
            validate_first=validate_first,
            dry_run=dry_run,
        )
        if offending_parameter is not None:
            raise_tool_error(
                create_validation_error(
                    _selector_only_parameter_message(offending_parameter),
                    parameter=offending_parameter,
                )
            )

        operations_list = _parse_bulk_operations(operations)
        await _reject_operations_group_member_conflicts(self._client, operations_list)

        result = await self._device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel, ctx=ctx
        )
        return cast(dict[str, Any], result)

    async def _run_bulk_selector(
        self,
        selector: dict[str, Any],
        *,
        action: str | None,
        parameters: dict[str, Any] | None,
        timeout_seconds: float | None,
        validate_first: bool,
        dry_run: bool,
        parallel: bool,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Resolve one structural selector and, unless ``dry_run``, dispatch it.

        Split out of ``ha_bulk_control`` to keep that tool's own McCabe
        complexity under the repo's C901 threshold
        (docs/agents/development.md — no per-file exemptions).
        """
        if action is None:
            raise_tool_error(
                create_validation_error(
                    "Selector mode requires action", parameter="action"
                )
            )
        try:
            parsed_selector = parse_json_param(selector, "selector")
            if not isinstance(parsed_selector, dict):
                raise BulkSelectorValidationError("selector must be a JSON object")
        except ValueError as exc:
            # BulkSelectorValidationError subclasses ValueError; catching it
            # separately alongside ValueError was redundant.
            parameter = getattr(exc, "parameter", "selector")
            raise_tool_error(create_validation_error(str(exc), parameter=parameter))
        try:
            resolution = await resolve_bulk_selector(
                self._client,
                parsed_selector,
                action=action,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                validate_first=validate_first,
            )
        except BulkSelectorValidationError as exc:
            # Caller-fixable: the selector itself is wrong. Unlike the
            # `except ValueError` above (which also catches plain ValueErrors
            # with no `.parameter`), every BulkSelectorValidationError sets
            # one (default "selector") -- no getattr fallback needed.
            raise_tool_error(create_validation_error(str(exc), parameter=exc.parameter))
        except BulkSelectorInfrastructureError as exc:
            # NOT caller-fixable by editing the selector. Routed through
            # create_connection_error (not create_validation_error) so an
            # agent doesn't try to rewrite a selector that was never the
            # problem -- but the specific next step depends on exc.cause,
            # since not every cause is actually a connectivity problem.
            logger.warning(
                "ha_bulk_control: selector resolution infrastructure failure (%s): %s",
                exc.cause,
                exc,
            )
            message, suggestions = _INFRASTRUCTURE_ERROR_SUGGESTIONS.get(
                exc.cause,
                _INFRASTRUCTURE_ERROR_SUGGESTIONS[
                    InfrastructureErrorCause.CONNECTIVITY
                ],
            )
            raise_tool_error(
                create_connection_error(
                    message, details=str(exc), suggestions=suggestions
                )
            )
        except Exception as exc:
            exception_to_structured_error(
                exc,
                context={"operation": "resolve bulk selector"},
            )
            raise  # unreachable: exception_to_structured_error always raises

        if dry_run:
            response: dict[str, Any] = {
                "success": True,
                "dry_run": True,
                "dispatched": False,
            }
            _attach_resolution_to_response(response, resolution)
            return response
        try:
            result = await self._device_tools.bulk_device_control(
                operations=resolution.operations,
                parallel=parallel,
                ctx=ctx,
            )
        except ToolError:
            # bulk_device_control already raises a fully structured ToolError
            # (e.g. "every operation failed validation" -- see its own
            # raise_tool_error call sites) -- re-raise it untouched. Without
            # this guard, the except Exception below would catch it too and
            # re-classify it via exception_to_structured_error, discarding
            # its real code/message/suggestions for a generic one.
            raise
        except Exception as exc:
            # Attach the frozen resolution to the error context: a
            # non-transactional, partially-executed bulk write must leave a
            # record of which entities were resolved and attempted, in the
            # response the agent sees, not just in whatever
            # bulk_device_control itself logged.
            logger.warning(
                "ha_bulk_control: dispatch failed after resolving %d entities: %s",
                len(resolution.resolved_entity_ids),
                exc,
            )
            exception_to_structured_error(
                exc,
                context={
                    "operation": "bulk device control",
                    "resolution": resolution.summary(),
                },
            )
            raise  # unreachable: exception_to_structured_error always raises
        response = cast(dict[str, Any], result)
        _attach_resolution_to_response(response, resolution)
        return response

    @property
    def events(self) -> RemoteEvents:
        return RemoteEvents(self._client)

    ha_call_event = tool(
        name="ha_call_event",
        tags={"Service & Device Control"},
        annotations=write_hints(
            "Call Event", destructive=True, idempotent=False, open_world=False
        ),
    )(log_tool_usage(legacy_errors(_EventContractTools.ha_call_event)))


def register_service_tools(
    mcp: Any, client: HomeAssistantClient, **kwargs: Any
) -> None:
    """Register service call and operation monitoring tools with the MCP server."""
    device_tools = kwargs.get("device_tools")
    if not device_tools:
        raise ValueError("device_tools is required for service tools registration")
    register_tool_methods(mcp, ServiceTools(client, device_tools))
