"""Remote compatibility acquisition for the shared structural selector algorithm."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..runtime.bulk_selection import (
    _NON_AGGREGATE_ROOT_DOMAINS as _NON_AGGREGATE_ROOT_DOMAINS,
)
from ..runtime.bulk_selection import (
    MAX_SELECTOR_ENTITIES as MAX_SELECTOR_ENTITIES,
)
from ..runtime.bulk_selection import (
    BulkControlSelector as BulkControlSelector,
)
from ..runtime.bulk_selection import (
    BulkSelectorInfrastructureError as BulkSelectorInfrastructureError,
)
from ..runtime.bulk_selection import (
    BulkSelectorResolution as BulkSelectorResolution,
)
from ..runtime.bulk_selection import (
    BulkSelectorValidationError as BulkSelectorValidationError,
)
from ..runtime.bulk_selection import (
    InfrastructureErrorCause as InfrastructureErrorCause,
)
from ..runtime.bulk_selection import (
    SelectorTopology as _Topology,
)
from ..runtime.bulk_selection import (
    _all_excluded_or_hidden_message as _all_excluded_or_hidden_message,
)
from ..runtime.bulk_selection import (
    _build_operation_common as _build_operation_common,
)
from ..runtime.bulk_selection import (
    _entity_area_id as _entity_area_id,
)
from ..runtime.bulk_selection import (
    _expand_entity as _expand_entity,
)
from ..runtime.bulk_selection import (
    _expand_roots as _expand_roots,
)
from ..runtime.bulk_selection import (
    _ExpansionEntry as _ExpansionEntry,
)
from ..runtime.bulk_selection import (
    _require_domain_known as _require_domain_known,
)
from ..runtime.bulk_selection import (
    _select_area_ids as _select_area_ids,
)
from ..runtime.bulk_selection import (
    _string_list as _string_list,
)
from ..runtime.bulk_selection import (
    _validate_device_registry_rows as _validate_device_registry_rows,
)
from ..runtime.bulk_selection import (
    _validate_selector as _validate_selector,
)
from ..runtime.bulk_selection import (
    _ValidatedSelector as _ValidatedSelector,
)
from ..runtime.bulk_selection import (
    prepare_bulk_selection,
    resolve_bulk_selection,
)
from ..visibility.resolver import VisibilityDataUnavailable, load_hidden_set

logger = logging.getLogger(__name__)


def _registry_rows(result: Any, label: str) -> list[dict[str, Any]]:
    """Unwrap one required Home Assistant registry response."""
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise BulkSelectorInfrastructureError(f"Home Assistant {label} is unavailable")
    rows = result.get("result")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BulkSelectorInfrastructureError(
            f"Home Assistant {label} returned an unexpected response"
        )
    return rows


async def _load_topology(client: HomeAssistantClient) -> _Topology:
    """Load the HA state and registry views used by one resolution.

    ``return_exceptions=True`` plus an explicit re-raise guard, mirroring
    ``tools_areas.py``'s registry fetch: a bare ``asyncio.gather`` lets the
    first exception propagate while the other four awaitables are neither
    cancelled nor retrieved, so their eventual exceptions surface only as an
    unattributed "Task exception was never retrieved" with no tool or call
    context once the event loop garbage-collects them.

    Logs every failed fetch here (not just the one that gets raised, and
    not left to the caller's own log line, which only ever sees the single
    exception that propagates): with two+ concurrent fetches failing (e.g.
    both the entity and device registry reads), every failure after the
    first would otherwise vanish without a trace. ``%r`` plus
    ``exc_info=`` (not ``%s``): ``str()`` on the exception types these
    fetches actually raise -- ``asyncio.TimeoutError``, ``ConnectionResetError``
    -- is frequently empty, which would otherwise produce a content-free
    WARNING line for exactly the outage this logging exists to diagnose.
    """
    results: tuple[Any, Any, Any, Any, Any] = await asyncio.gather(
        client.get_states(),
        client.send_websocket_message({"type": "config/entity_registry/list"}),
        client.send_websocket_message({"type": "config/device_registry/list"}),
        client.send_websocket_message({"type": "config/area_registry/list"}),
        client.send_websocket_message({"type": "config/floor_registry/list"}),
        return_exceptions=True,
    )
    states, entities, devices, areas, floors = results
    # Labels paired with their named local, not zipped against `results` by
    # position: a `zip(strict=True)` against a separate label tuple only
    # catches a LENGTH change on reorder, not the reorder itself -- if a
    # future edit swaps two `gather()` lines and correctly updates the
    # unpack above to match, a position-based label tuple would silently
    # keep naming the OLD order, confidently blaming the wrong registry in
    # the WARNING below during exactly the outage this logging exists to
    # diagnose. Binding each label directly to its already-correct local
    # makes a mislabel impossible without an edit that is visibly wrong on
    # this line itself.
    labeled_results = (
        ("states", states),
        ("entity registry", entities),
        ("device registry", devices),
        ("area registry", areas),
        ("floor registry", floors),
    )
    failures = [
        (label, result)
        for label, result in labeled_results
        if isinstance(result, BaseException)
    ]
    if failures:
        for label, failure in failures:
            logger.warning(
                "bulk selector: %s fetch failed: %r", label, failure, exc_info=failure
            )
        raise failures[0][1]
    # Keyword construction, not `_Topology(*results)`: the NamedTuple exists
    # specifically so a reordered gather() call can't mislabel a registry
    # silently -- unpacking the gather() result and re-splatting it
    # positionally here would carry the exact risk it was written to avoid.
    return _Topology(
        states=states,
        entities=_registry_rows(entities, "entity registry"),
        devices=_registry_rows(devices, "device registry"),
        areas=_registry_rows(areas, "area registry"),
        floors=_registry_rows(floors, "floor registry"),
    )


async def _load_hidden_entities(
    client: Any,
    entity_result: Any,
    states_result: list[dict[str, Any]],
    device_result: Any,
    entity_registry: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], list[str]]:
    """Load visibility policy strictly and include registry-hidden entities.

    Returns ``(hidden_ids, warnings)``. The warnings are operator-facing
    visibility-config notes (e.g. an unknown ``exclude_categories`` entry)
    that ``load_hidden_set`` still returns even under ``strict=True`` --
    per its own docstring, "benign notes ... still warn, never raise" is
    orthogonal to the strict/fail-closed behavior, which only covers
    degradations that would otherwise leak entities. Threaded into the
    resolution's own ``warnings`` by the caller rather than discarded, so
    whoever owns ``entity_visibility.json`` learns about a config problem
    this resolution surfaced on their behalf.
    """
    try:
        visibility_hidden, warnings = await load_hidden_set(
            entity_result,
            states_result,
            client,
            device_result,
            strict=True,
        )
    except VisibilityDataUnavailable as exc:
        # Preserve and log the underlying cause: VisibilityDataUnavailable
        # covers four distinct degradations (registry unusable, allowlist
        # registry empty, Assist data unavailable, config load failure) --
        # collapsing them to one fixed sentence here would erase which one
        # actually happened, and nothing else records it server-side.
        logger.warning("bulk selector: visibility resolution failed: %s", exc)
        raise BulkSelectorInfrastructureError(
            f"Entity visibility could not be resolved safely: {exc}",
            # "visibility_config", not the "connectivity" default: one of
            # the four VisibilityDataUnavailable causes this wraps is a
            # corrupt/unloadable entity_visibility.json in the user's own
            # data dir -- not distinguished from the other three (HA
            # registry/Assist-data unavailable) here, so the routed
            # suggestions below hedge across both rather than confidently
            # pointing at the wrong one.
            cause=InfrastructureErrorCause.VISIBILITY_CONFIG,
        ) from exc
    registry_hidden = {
        entity_id
        for entity_id, row in entity_registry.items()
        if row.get("hidden_by") is not None
    }
    return visibility_hidden | registry_hidden, warnings


async def resolve_bulk_selector(
    client: HomeAssistantClient,
    selector: Mapping[str, Any],
    *,
    action: str,
    parameters: dict[str, Any] | None,
    timeout_seconds: float | None,
    validate_first: bool,
) -> BulkSelectorResolution:
    """Resolve exact HA topology to visible, non-excluded leaf operations.

    All registry/state reads and validation finish before the caller receives any
    operation row, so a malformed selector can never produce a partial dispatch.
    Membership is integration-neutral and uses the shared normalized attributes
    supplied by Home Assistant; no manufacturer, model, or integration heuristics
    apply. The one domain-level exception is ``scene`` (see
    ``_NON_AGGREGATE_ROOT_DOMAINS``): HA core gives that domain's ``entity_id``
    attribute a different meaning (controlled targets, not structural members),
    so it is excluded from aggregate-root admission on architectural grounds,
    not integration identity.
    """
    _validate_selector(selector, action)
    topology = await _load_topology(client)
    prepared = prepare_bulk_selection(topology, selector, action)
    hidden, warnings = await _load_hidden_entities(
        client,
        {"success": True, "result": topology.entities},
        topology.states,
        {"success": True, "result": topology.devices},
        prepared.entity_registry,
    )
    return resolve_bulk_selection(
        prepared,
        hidden=hidden,
        visibility_warnings=warnings,
        parameters=parameters,
        timeout_seconds=timeout_seconds,
        validate_first=validate_first,
    )
