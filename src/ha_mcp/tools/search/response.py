"""Compatibility exports plus the retained remote snapshot acquisition."""

import asyncio
from typing import Any

from ...backends.legacy import legacy_errors_sync
from ...runtime.search_response import (
    _ALWAYS_KEEP_PROJECTION,
    _CONFIG_BUCKETS,
    _ENTITIES_BRANCH_SKIP_KEYS,
    _INTENT_SKIP_WARNING,
    _VALID_SEARCH_TYPES,
    _apply_by_domain_grouping,
    _apply_result_fields_to_response,
    _apply_search_outcome,
    _build_hidden_ids,
    _build_pagination_metadata,
    _compute_eligibility,
    _effective_result_fields,
    _emit_intent_skip_warning,
    _finalize_partial_state,
    _merge_payload_metadata,
    _mirror_partial_to_warnings,
    _new_search_response,
    _project_records,
    _project_response_fields,
    _ResolvedSearch,
    _synthesize_combined_pagination,
)
from ...runtime.search_response import (
    _validate_search_types as _shared_validate_search_types,
)

_validate_search_types = legacy_errors_sync(_shared_validate_search_types)


async def _prefetch_shared_search_snapshots(
    client: Any,
    *,
    registry_eligible: bool,
    body_eligible: bool,
) -> tuple[list[dict[str, Any]] | None, Any]:
    """Pre-fetch ``/api/states`` + the entity-registry list once for ha_search.

    Only pre-fetches when both branches are eligible — a lone branch keeps
    fetching for itself. Returns ``(shared_states, shared_registry)``; either is
    ``None`` when not pre-fetched, or when its fetch failed (each branch then
    fetches and fails on its own, reproducing the per-surface partial-result
    handling exactly). A cancellation (structured-concurrency teardown)
    propagates rather than degrading to per-branch fetching.
    """
    if not (registry_eligible and body_eligible):
        return None, None
    prefetch = await asyncio.gather(
        client.get_states(),
        client.send_websocket_message({"type": "config/entity_registry/list"}),
        return_exceptions=True,
    )
    for snap in prefetch:
        if isinstance(snap, BaseException) and not isinstance(snap, Exception):
            raise snap
    shared_states = prefetch[0] if not isinstance(prefetch[0], BaseException) else None
    shared_registry = (
        prefetch[1] if not isinstance(prefetch[1], BaseException) else None
    )
    return shared_states, shared_registry


__all__ = [
    "_ALWAYS_KEEP_PROJECTION",
    "_CONFIG_BUCKETS",
    "_ENTITIES_BRANCH_SKIP_KEYS",
    "_INTENT_SKIP_WARNING",
    "_ResolvedSearch",
    "_VALID_SEARCH_TYPES",
    "_apply_by_domain_grouping",
    "_apply_result_fields_to_response",
    "_apply_search_outcome",
    "_build_hidden_ids",
    "_build_pagination_metadata",
    "_compute_eligibility",
    "_effective_result_fields",
    "_emit_intent_skip_warning",
    "_finalize_partial_state",
    "_merge_payload_metadata",
    "_mirror_partial_to_warnings",
    "_new_search_response",
    "_project_records",
    "_project_response_fields",
    "_synthesize_combined_pagination",
    "_validate_search_types",
]
