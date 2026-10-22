"""Pure bulk request checks and visibility-safe resolution presentation."""

from typing import Any, NamedTuple

from ha_mcp.errors import create_validation_error
from ha_mcp.utils.entity_membership import normalize_member_entity_ids

from .bulk_selection import (
    _NON_AGGREGATE_ROOT_DOMAINS,
    BulkSelectorResolution,
    InfrastructureErrorCause,
)
from .errors import raise_failure


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


def reject_group_member_conflicts(entity_ids: set[str], states: list[Any]) -> None:
    """Reject a frozen batch targeting both an aggregate and its own members."""
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
    raise_failure(
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
