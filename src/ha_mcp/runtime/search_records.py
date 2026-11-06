"""Search record projection and membership rules, without transport imports."""

from collections.abc import Mapping
from typing import Any

from ha_mcp.errors import create_validation_error
from ha_mcp.utils.entity_membership import normalize_member_entity_ids

from .errors import raise_failure

_ENTITY_RECORD_KEYS = (
    "entity_id",
    "friendly_name",
    "domain",
    "state",
    "score",
    "match_type",
)


_ENRICHMENT_FIELDS: tuple[str, ...] = ("area", "floor", "labels", "aliases")


_MEMBERSHIP_FIELDS: tuple[str, ...] = ("is_group", "member_entity_ids")


_ALLOWED_RESULT_FIELDS: frozenset[str] = (
    frozenset(_ENTITY_RECORD_KEYS)
    | frozenset(_ENRICHMENT_FIELDS)
    | frozenset(_MEMBERSHIP_FIELDS)
)


def _validate_result_field_names(parsed: list[str] | None) -> None:
    """Reject unknown ``result_fields`` names with the standard validation error.

    ``result_fields`` now drives area/floor/labels/aliases enrichment (issue #1813
    C1), so an unrecognised name is a hard error rather than a silently-empty
    projection: the server must know which fields to compute. Empty is rejected too
    (omit the parameter for full records). Called once in ``ha_search`` so both the
    component and legacy serving paths share one contract.
    """
    if parsed is None:
        return
    if not parsed:
        raise_failure(
            create_validation_error(
                "result_fields must contain at least one key; omit the parameter "
                "for full records.",
                parameter="result_fields",
            )
        )
    unknown = [f for f in parsed if f not in _ALLOWED_RESULT_FIELDS]
    if unknown:
        raise_failure(
            create_validation_error(
                f"Unknown result_fields: {unknown}. "
                f"Valid keys: {sorted(_ALLOWED_RESULT_FIELDS)}.",
                parameter="result_fields",
            )
        )


def _requested_enrichment(parsed_result_fields: list[str] | None) -> tuple[str, ...]:
    """The enrichment fields named in ``result_fields``, in canonical order.

    Empty when ``result_fields`` is unset or names only base record keys — the
    signal that no enrichment work (component key retention or a legacy registry
    join) is needed, keeping the default search path cost-free.
    """
    if not parsed_result_fields:
        return ()
    requested = set(parsed_result_fields)
    return tuple(f for f in _ENRICHMENT_FIELDS if f in requested)


def _requested_membership(parsed_result_fields: list[str] | None) -> tuple[str, ...]:
    """Return requested membership fields, retaining the group discriminator.

    A member-only projection still includes is_group so a redacted group,
    an empty group, and a leaf entity remain distinguishable.
    """
    if not parsed_result_fields:
        return ()
    requested = set(parsed_result_fields)
    if "member_entity_ids" in requested:
        requested.add("is_group")
    return tuple(field for field in _MEMBERSHIP_FIELDS if field in requested)


def _add_membership_fields(
    record: dict[str, Any],
    attributes: Any,
    requested: tuple[str, ...],
    *,
    denied_member_ids: set[str],
) -> None:
    """Add requested generic membership metadata to one search record.

    Smart-search paths may mark attributes in _redact_hidden_memberships before
    this final projection. Callers that consume that sentinel pass an explicit
    empty denied set.
    """
    if not requested:
        return
    members = normalize_member_entity_ids(attributes)
    redacted = bool(
        isinstance(attributes, Mapping)
        and attributes.get("_ha_mcp_membership_redacted")
    ) or bool(
        members is not None
        and denied_member_ids
        and denied_member_ids.intersection(members)
    )
    if "is_group" in requested:
        record["is_group"] = members is not None
    if "member_entity_ids" in requested and members is not None and not redacted:
        record["member_entity_ids"] = members
