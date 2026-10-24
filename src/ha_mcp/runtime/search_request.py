"""Resolve the two search surfaces once for both native and retained clients."""

from typing import Any

from ha_mcp.errors import create_validation_error
from ha_mcp.tools.coercion import parse_string_list_param

from .errors import raise_failure
from .search_records import _validate_result_field_names
from .search_response import (
    _compute_eligibility,
    _ResolvedSearch,
    _validate_search_types,
)


def resolve_search_request(arguments: dict[str, Any]) -> _ResolvedSearch:
    try:
        parsed_search_types = parse_string_list_param(
            arguments["search_types"], "search_types"
        )
    except ValueError as exc:
        raise_failure(create_validation_error(str(exc), parameter="search_types"))
    _validate_search_types(parsed_search_types)
    try:
        parsed_fields = parse_string_list_param(
            arguments["fields"], "fields", allow_csv=True
        )
    except ValueError as exc:
        raise_failure(create_validation_error(str(exc), parameter="fields"))
    try:
        parsed_result_fields = parse_string_list_param(
            arguments["result_fields"], "result_fields", allow_csv=True
        )
    except ValueError as exc:
        raise_failure(create_validation_error(str(exc), parameter="result_fields"))
    _validate_result_field_names(parsed_result_fields)
    query_text = (arguments["query"] or "").strip()
    domain_filter_text = (arguments["domain_filter"] or "").strip()
    area_filter_text = (arguments["area_filter"] or "").strip()
    state_filter_text = (arguments["state_filter"] or "").strip()
    explicit_config_only = parsed_search_types is not None
    registry_eligible, body_eligible, body_skipped_by_intent_gate = (
        _compute_eligibility(
            query_text=query_text,
            domain_filter_text=domain_filter_text,
            area_filter_text=area_filter_text,
            state_filter_text=state_filter_text,
            explicit_config_only=explicit_config_only,
        )
    )
    if not registry_eligible and (not body_eligible):
        raise_failure(
            create_validation_error(
                "ha_search requires a non-empty query, or one of domain_filter / area_filter / state_filter to enumerate.",
                parameter="query",
            )
        )
    req = _ResolvedSearch(
        query=arguments["query"],
        query_text=query_text,
        domain_filter=arguments["domain_filter"],
        area_filter=arguments["area_filter"],
        state_filter=arguments["state_filter"],
        parsed_search_types=parsed_search_types,
        parsed_fields=parsed_fields,
        result_fields=arguments["result_fields"],
        limit=arguments["limit"],
        offset=arguments["offset"],
        exact_match=arguments["exact_match"],
        include_hidden=arguments["include_hidden"],
        include_config=arguments["include_config"],
        group_by_domain=arguments["group_by_domain"],
        per_domain_limit=arguments["per_domain_limit"],
        config_time_budget=arguments["config_time_budget"],
        registry_eligible=registry_eligible,
        body_eligible=body_eligible,
        body_skipped_by_intent_gate=body_skipped_by_intent_gate,
    )
    return req
