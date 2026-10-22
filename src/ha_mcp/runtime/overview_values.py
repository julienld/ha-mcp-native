"""Pure overview value reductions, without process-global configuration."""

from dataclasses import dataclass
from typing import Any

from ha_mcp.errors import create_validation_error
from ha_mcp.tools.coercion import parse_string_list_param

from .errors import raise_failure


def _simplify_states_summary(
    states_summary: dict[str, int],
    detail_level: str,
    max_states: int | None = None,
) -> dict[str, int]:
    """Keep only the most common states, aggregate the rest into _other.

    Args:
        states_summary: Original {state: count} mapping.
        detail_level: "minimal", "standard", or "full".
        max_states: Override cap (None = 5 for minimal, 10 for standard).

    Returns:
        Capped states_summary with ``_other`` count when truncated.
    """
    if detail_level == "full":
        return states_summary

    if max_states is None:
        max_states = 5 if detail_level == "minimal" else 10

    if len(states_summary) <= max_states:
        return states_summary

    sorted_states = sorted(states_summary.items(), key=lambda x: x[1], reverse=True)
    top = dict(sorted_states[:max_states])
    other_count = sum(count for _, count in sorted_states[max_states:])
    if other_count > 0:
        top["_other"] = other_count
    return top


@dataclass(frozen=True)
class _OverviewInputs:
    """Resolved ``ha_get_overview`` inputs threaded to the routing/assembly.

    All display params (``detail_level`` … ``offset``) stay server-side — the
    component returns raw slices independent of them; the two include-flags gate
    which slices it bothers to snapshot.
    """

    detail_level: str
    max_entities_per_domain: int | None
    include_state: bool | None
    include_entity_id: bool | None
    domains_filter: list[str] | None
    limit: int | None
    offset: int
    include_notifications: bool
    include_dismissed_repairs: bool


_OVERVIEW_INDEPENDENT_FIELDS = frozenset(
    {
        "success",
        "system_info",
        "notification_count",
        "notifications",
        "repair_count",
        "dismissed_repair_count",
        "repairs",
        "repairs_error",
        "tool_discovery",
        "settings_url",
        "settings_url_hint",
        "read_only_mode",
        "read_only_mode_hint",
        "ha_mcp_update",
    }
)

_OVERVIEW_NOTIFICATION_FIELDS = frozenset({"notification_count", "notifications"})

_OVERVIEW_REPAIR_FIELDS = frozenset(
    {
        "repair_count",
        "dismissed_repair_count",
        "repairs",
        "repairs_error",
    }
)

_OVERVIEW_ENTITY_FIELDS = frozenset(
    {
        "system_summary",
        "domain_stats",
        "area_analysis",
        "ai_insights",
        "pagination",
        "partial",
        "warnings",
        "device_types",
        "service_availability",
    }
)

_OVERVIEW_AVAILABLE_FIELDS = _OVERVIEW_INDEPENDENT_FIELDS | _OVERVIEW_ENTITY_FIELDS


def resolve_overview_request(
    **arguments: Any,
) -> tuple[_OverviewInputs, list[str] | None, bool]:
    parsed: dict[str, list[str] | None] = {}
    for name in ("fields", "domains"):
        try:
            parsed[name] = parse_string_list_param(
                arguments.get(name), name, allow_csv=True
            )
        except ValueError as error:
            raise_failure(create_validation_error(str(error), parameter=name))
    inputs = _OverviewInputs(
        detail_level=arguments["detail_level"],
        max_entities_per_domain=arguments["max_entities_per_domain"],
        include_state=arguments["include_state"],
        include_entity_id=arguments["include_entity_id"],
        domains_filter=parsed["domains"],
        limit=arguments["limit"],
        offset=arguments["offset"],
        include_notifications=arguments["include_notifications"]
        if arguments["include_notifications"] is not None
        else True,
        include_dismissed_repairs=bool(arguments["include_dismissed_repairs"]),
    )
    independent = parsed["fields"] is not None and not (
        set(parsed["fields"]) & _OVERVIEW_ENTITY_FIELDS
    )
    return inputs, parsed["fields"], independent
