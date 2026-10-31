"""Search window merging and response projections, independent of transports."""

import logging
from dataclasses import dataclass
from typing import Any

from .search_records import (
    _ENTITY_RECORD_KEYS,
    _requested_enrichment,
    _requested_membership,
)
from .search_response import (
    _CONFIG_BUCKETS,
    _apply_by_domain_grouping,
    _apply_result_fields_to_response,
    _apply_search_outcome,
    _as_record_list,
    _dashboard_failure_reason,
    _emit_intent_skip_warning,
    _finalize_partial_state,
    _format_search_diagnostics,
    _merge_partial_reason,
    _mirror_partial_to_warnings,
    _new_search_response,
    _normalized_domain_filter,
    _parse_component_result_fields,
    _project_response_fields,
    _ResolvedSearch,
    _synthesize_combined_pagination,
    merge_visibility_warnings,
)

logger = logging.getLogger(__name__)

_DASHBOARD_SEARCH_TYPE = "dashboard"


_COMPONENT_MAX_RESULTS = 500


def _normalize_component_config_record(
    bucket: str,
    rec: dict[str, Any],
    include_config: bool,
    *,
    preserve_core_fields: bool = False,
) -> dict[str, Any]:
    """Map one component config-bucket record onto the exact legacy key set.

    The component speaks HA-native vocabulary (automations/scripts carry an
    ``alias``, scenes a ``name``, storage ids ride an ``id`` key, and records
    add ``source``/``kind``/``object_id`` metadata). The legacy deep-search
    records that agents, tests, and downstream consumers key on use
    ``friendly_name`` plus per-bucket id keys (``script_id``/``scene_id``) —
    so normalize here, at the single seam, rather than teaching the component
    the MCP envelope's vocabulary. Extra component fields are dropped to keep
    the legacy key set, except ``options_withheld``, which marks a custom
    helper whose options the component did not read; enrichment
    (e.g. ``source: yaml``) can be added to BOTH paths together later.

    ``config`` key semantics mirror the legacy pipeline's include_config pop:
    present (possibly ``None`` for YAML/name-only matches) when
    ``include_config`` is True, absent otherwise. Flow-helper records carry
    their body under ``options`` component-side (data-minimized
    ``ConfigEntry.options``); legacy calls the same payload ``config``.
    """
    entity_id = rec.get("entity_id")
    name = rec.get("alias") or rec.get("name") or rec.get("friendly_name")
    out: dict[str, Any] = dict(rec) if preserve_core_fields else {}
    # Keep one body key, and never let native-field preservation defeat its gate.
    out.pop("options", None)
    out.pop("config", None)
    if bucket == "helpers":
        if rec.get("kind") == "flow" or (entity_id is None and rec.get("entry_id")):
            out["entry_id"] = rec.get("entry_id")
        else:
            out["entity_id"] = entity_id
        out["helper_type"] = rec.get("helper_type")
        out["name"] = name
        if rec.get("options_withheld") is not None:
            out["options_withheld"] = rec["options_withheld"]
    else:
        out["entity_id"] = entity_id
        if bucket == "scripts":
            out["script_id"] = rec.get("id", rec.get("script_id"))
        elif bucket == "scenes":
            out["scene_id"] = rec.get("id", rec.get("scene_id"))
        out["friendly_name"] = name if name is not None else entity_id
    out["score"] = rec.get("score")
    out["match_in_name"] = bool(rec.get("match_in_name"))
    out["match_in_config"] = bool(rec.get("match_in_config"))
    # True when Home Assistant's reference graph named this config as
    # referencing the queried entity (#2258). Independent of
    # ``match_in_config``: a config the graph confirms but whose body was
    # unreadable carries this flag alone. The standalone command bridge does
    # not consult that graph; the native owner joins it before this projection.
    # This flag says "the graph named this one", never "no references exist".
    out["match_in_references"] = bool(rec.get("match_in_references"))
    if include_config:
        config = rec.get("config")
        if config is None and "options" in rec:
            config = rec.get("options")
        out["config"] = config if config else None
    return out


_COMPONENT_BODY_SEARCH_TYPES: frozenset[str] = frozenset(
    {"automation", "script", "scene", "helper"}
)


_COMPONENT_CONFIG_BUCKETS: tuple[str, ...] = tuple(
    bucket for bucket in _CONFIG_BUCKETS if bucket != "dashboards"
)


def _component_serves_search_types(req: _ResolvedSearch) -> bool:
    """True when the component route can serve every requested surface.

    Only an explicit ``search_types`` list can name a surface the component's
    ``search`` command lacks, and only the body-eligible branch forwards it — a
    body-ineligible request sends the entity surface alone, which the component
    always accepts.

    ``dashboard`` is served ALONGSIDE the command rather than by it: the wire
    request drops the value (``_build_component_search_request``) and the
    dashboards leg fills the bucket, merged server-side (issue #2289). Any
    other unsupported surface still sends the whole request to the legacy path,
    silently — the same treatment as the other route-ineligible modes, not the
    warning-emitting failure fallback.
    """
    if not req.body_eligible or req.parsed_search_types is None:
        return True
    return all(
        t in _COMPONENT_BODY_SEARCH_TYPES or t == _DASHBOARD_SEARCH_TYPE
        for t in req.parsed_search_types
    )


def _component_body_search_types(req: _ResolvedSearch) -> list[str]:
    """The body surfaces forwarded to the component, ``dashboard`` stripped.

    The component's ``search`` schema rejects ``dashboard``, so the value never
    crosses the wire even when the caller asked for it — the dashboards leg
    serves that bucket instead (issue #2289).
    """
    parsed = req.parsed_search_types or ["automation", "script", "scene", "helper"]
    return [t for t in parsed if t != _DASHBOARD_SEARCH_TYPE]


def _build_component_search_request(
    req: _ResolvedSearch, *, dashboard_split: bool = False
) -> dict[str, Any]:
    """Translate resolved ha_search inputs into an ``ha_mcp_tools/search`` request.

    ``search_types`` on the WS command selects surfaces including the entity
    surface (``"entity"``), so branch eligibility computed server-side maps
    directly onto which surfaces the component searches. Optional string
    filters are omitted when empty to satisfy the component's ``str``-typed
    voluptuous schema.

    ``dashboard_split`` switches to the WINDOW fetch the dashboard merge needs.
    The component pages its own corpus, so the server can only page the MERGED
    list if it holds every component record that could reach the caller's page
    — that is the whole ``[0, offset + limit)`` prefix. With the default
    ``offset=0`` the window is byte-identical to the plain request.
    """
    search_types: list[str] = []
    if req.registry_eligible:
        search_types.append("entity")
    if req.body_eligible:
        search_types.extend(_component_body_search_types(req))
    request: dict[str, Any] = {
        "search_types": search_types,
        "exact": req.exact_match,
        "include_hidden": req.include_hidden,
        "include_config": req.include_config,
        "limit": (req.offset + req.limit) if dashboard_split else req.limit,
        "offset": 0 if dashboard_split else req.offset,
    }
    membership_fields = _requested_membership(
        _parse_component_result_fields(req.result_fields)
    )
    if membership_fields:
        request["result_fields"] = list(membership_fields)
    if req.query_text:
        request["query"] = req.query_text
    domain_filter = _normalized_domain_filter(req.domain_filter)
    if domain_filter:
        request["domain_filter"] = domain_filter
    area_filter = (req.area_filter or "").strip()
    if area_filter:
        request["area_filter"] = area_filter
    state_filter = (req.state_filter or "").strip()
    if state_filter:
        request["state_filter"] = state_filter
    return request


def _dashboard_split_requested(req: _ResolvedSearch) -> bool:
    """True when this request needs the dashboards leg merged in (issue #2289)."""
    return bool(
        req.body_eligible
        and req.parsed_search_types is not None
        and _DASHBOARD_SEARCH_TYPE in req.parsed_search_types
    )


def _component_max_results(caps: Any) -> int:
    """The component's advertised ``limits.max_results``, defaulting to 500.

    The advisory ``limits`` ride the cached ``info`` probe, so a component
    advertising a smaller ceiling gates the window fetch here instead of
    accepting the split and then rejecting the frame in its schema (an
    avoidable failed round-trip whose fallback still serves the caller).
    """
    limits = getattr(caps, "limits", None)
    value = limits.get("max_results") if isinstance(limits, dict) else None
    if isinstance(value, int) and value > 0:
        return value
    return _COMPONENT_MAX_RESULTS


@dataclass(frozen=True)
class _DashboardLeg:
    """The dashboards surface's contribution to a component-served ha_search.

    ``records`` are legacy-shaped (``url_path`` / ``title`` /
    ``score``), NOT component records — they never pass through
    ``_normalize_component_config_record``. ``failed`` is the leg's own
    "not scanned" count, reported with the deep path's wording. ``error`` is
    set only when the leg raised, in which case the bucket is empty and the
    response says so rather than reading as a clean zero.
    """

    records: list[dict[str, Any]]
    failed: int = 0
    error: str | None = None


def _apply_entity_window(req: _ResolvedSearch, windowed: dict[str, Any]) -> None:
    """Re-slice the over-fetched entity surface onto the caller's page.

    The window fetch over-fetches every surface the request names. Entity
    records merge with nothing, so their page is the same slice applied
    server-side. ``entity_total_matches`` is a corpus total and
    pagination-independent, so ``entity_has_more`` is recomputed against it
    (and pinned, so the shaping helper never falls back to the sliced length).

    No CURRENT request gets past the early return: the split needs an explicit
    ``search_types``, which pins the call config-only
    (``_compute_eligibility``), so the window carries no entity surface at all.
    This is the guard that keeps the window honest if that eligibility ever
    admits one — without it the entity page would silently be the whole
    ``[0, offset + limit)`` prefix.
    """
    if not req.registry_eligible:
        return
    entities = _as_record_list(windowed.get("entities"))
    total = int(windowed.get("entity_total_matches", len(entities)) or 0)
    page = entities[req.offset : req.offset + req.limit]
    windowed["entities"] = page
    windowed["entity_total_matches"] = total
    windowed["entity_has_more"] = (req.offset + len(page)) < total


def _merge_sort_key(record: dict[str, Any]) -> str:
    """Mirror of the component's ``_sort_key`` config tiebreak, dashboards added.

    The component cuts its window with ``(-score, _sort_key)`` where
    ``_sort_key`` is ``str(entity_id or id or name or "")``
    (``custom_components/ha_mcp_tools/websocket_api/search.py``); those fields ride
    the wire records unchanged, so the merge reproduces the exact order that
    decided window membership. Dashboard records are outside the component
    corpus, so any deterministic key places them consistently across pages —
    ``url_path`` extends the same chain.
    """
    return str(
        record.get("entity_id")
        or record.get("id")
        or record.get("name")
        or record.get("url_path")
        or ""
    )


def _merge_dashboard_window(
    req: _ResolvedSearch,
    component_result: dict[str, Any],
    dashboard_records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Page the window fetch and the dashboards leg as ONE score-sorted list.

    The legacy contract is a global score sort across every config bucket, then
    ``[offset : offset + limit]`` (``_deep._paginate_and_build_response``), so
    the merged page has to be cut server-side. It is exact: the component
    fetched the whole ``[0, offset + limit)`` window of its own corpus, a
    superset of every component record that can reach the caller's page, and
    the leg returns its bucket unpaginated.

    The cut uses the SAME total order that selected the window —
    ``(-score, _merge_sort_key)``, mirroring the component's
    ``(-score, _sort_key)`` — because window membership is decided by the
    component's order: cutting the merge with any other order lets an
    equal-score record at a window boundary repeat on one page and vanish
    from the next as ``offset`` grows.

    Returns the component result rewritten for the caller's page — buckets
    sliced, entity records re-sliced, totals and ``*_has_more`` recomputed —
    plus the dashboards page.
    """
    tagged: list[tuple[str, dict[str, Any]]] = [
        (bucket, record)
        for bucket in _COMPONENT_CONFIG_BUCKETS
        if bucket in component_result
        for record in _as_record_list(component_result.get(bucket))
    ]
    tagged.extend(("dashboards", record) for record in dashboard_records)
    tagged.sort(
        key=lambda item: (-(item[1].get("score") or 0), _merge_sort_key(item[1]))
    )
    page = tagged[req.offset : req.offset + req.limit]

    total = int(component_result.get("config_total_matches", 0) or 0) + len(
        dashboard_records
    )
    windowed: dict[str, Any] = dict(component_result)
    for bucket in _COMPONENT_CONFIG_BUCKETS:
        if bucket in component_result:
            windowed[bucket] = [rec for name, rec in page if name == bucket]
    windowed["config_total_matches"] = total
    # The component's own flag still carries information the merged count
    # cannot: it reports a corpus extending past the window, whose records are
    # not in the merged list at all.
    windowed["config_has_more"] = (req.offset + len(page)) < total or bool(
        component_result.get("config_has_more")
    )
    _apply_entity_window(req, windowed)

    dashboards_page = [rec for name, rec in page if name == "dashboards"]
    if not req.include_config:
        # Mirrors the legacy pipeline's per-record pop, which also runs on the
        # paginated slice rather than the corpus.
        for record in dashboards_page:
            record.pop("config", None)
    return windowed, dashboards_page


def _apply_dashboard_leg_state(response: dict[str, Any], leg: _DashboardLeg) -> None:
    """Fold the dashboards leg's incompleteness into the merged response.

    ``failed`` reuses the deep path's own per-type wording verbatim, so a
    merged response and a legacy one report the same sentence for the same
    condition. A raised leg is reported like a failed legacy branch — an
    ``errors[]`` entry naming the surface plus ``partial`` — so an empty
    dashboards bucket is never mistaken for a clean zero.
    """
    if leg.failed:
        response["partial"] = True
        _merge_partial_reason(response, _dashboard_failure_reason(leg.failed))
    if leg.error is not None:
        _finalize_partial_state(
            response,
            partial_local=True,
            errors_local=[{"surface": "dashboards", "error": leg.error}],
        )


def _merge_component_visibility_warnings(
    response: dict[str, Any], component_result: dict[str, Any]
) -> None:
    """Fold the component's visibility, location and helper warnings into the response.

    The component emits these when a hide dimension fails open (unknown category /
    empty-registry allowlist / Assist unavailable), when a location scan is
    incomplete, or when a helper search ran degraded: secrets.yaml could not be
    read (options or matches went unscrubbed), or Core's helper flow list could
    not be read (custom helper integrations are missing). Merged into the same top-level
    warnings surface the legacy path fills via ``merge_visibility_warnings``, so the
    fast path is no longer silent about incomplete filtering.
    """
    component_visibility_warnings = component_result.get("visibility_warnings")
    if isinstance(component_visibility_warnings, list):
        merge_visibility_warnings(
            response,
            [w for w in component_visibility_warnings if isinstance(w, str)],
        )
    elif component_visibility_warnings is not None:
        logger.warning(
            "component visibility_warnings ignored: expected a list, got %s",
            type(component_visibility_warnings).__name__,
        )

    component_warnings = component_result.get("warnings")
    if isinstance(component_warnings, list):
        merge_visibility_warnings(
            response,
            [warning for warning in component_warnings if isinstance(warning, str)],
        )


def _component_config_payload(
    req: _ResolvedSearch,
    component_result: dict[str, Any],
    dashboard_leg: _DashboardLeg | None,
    *,
    preserve_core_fields: bool = False,
) -> dict[str, Any]:
    """Build the config-surface payload ``_apply_search_outcome`` consumes.

    Component records are normalized into the legacy vocabulary; the
    dashboards leg's records are already legacy-shaped, so they are attached
    as-is (issue #2289).
    """
    config_has_more = bool(component_result.get("config_has_more", False))
    payload: dict[str, Any] = {
        "total_matches": int(component_result.get("config_total_matches", 0) or 0),
        "has_more": config_has_more,
        "next_offset": (req.offset + req.limit) if config_has_more else None,
    }
    for bucket in _CONFIG_BUCKETS:
        if bucket in component_result:
            payload[bucket] = [
                _normalize_component_config_record(
                    bucket,
                    rec,
                    req.include_config,
                    preserve_core_fields=preserve_core_fields,
                )
                for rec in _as_record_list(component_result.get(bucket))
            ]
    if dashboard_leg is not None:
        payload["dashboards"] = dashboard_leg.records
    return payload


def _component_entity_search_mode(req: _ResolvedSearch) -> str:
    """Retain each public listing mode while using component matching."""
    if (req.area_filter or "").strip():
        return "area_filtered_query" if req.query_text else "area_only"
    if req.query_text:
        return "exact_match" if req.exact_match else "fuzzy_search"
    return "domain_listing" if (req.domain_filter or "").strip() else "state_listing"


def _component_listing_metadata(
    req: _ResolvedSearch, payload: dict[str, Any], component_result: dict[str, Any]
) -> None:
    """Preserve public listing labels and empty-area messages without rescanning."""
    domain = _normalized_domain_filter(req.domain_filter)
    if domain:
        payload["domain_filter"] = domain
    area = (req.area_filter or "").strip()
    if area:
        payload["area_filter"] = area
        payload["area_names"] = component_result.get("area_names", [])
        if not payload["total_matches"]:
            qualifier = f"{domain} " if domain else ""
            payload["message"] = f"No {qualifier}entities found in area: {area}"
    if not req.query_text:
        match_type = "area_match" if area else payload["search_type"]
        for entity in payload["results"]:
            entity["match_type"] = match_type


def _shape_component_search_response(
    req: _ResolvedSearch,
    component_result: dict[str, Any],
    *,
    dashboard_leg: _DashboardLeg | None = None,
    preserve_core_fields: bool = False,
) -> dict[str, Any]:
    """Map an ``ha_mcp_tools/search`` result into the ha_search envelope.

    The component returns per-surface records already scored and paginated
    (``entities`` + config buckets with ``*_total_matches`` / ``*_has_more``).
    Projection (``result_fields`` on entity records, ``fields`` on the
    response), by-domain grouping, and the flat pagination / partial-mirror
    finalisation all stay server-side and reuse the same helpers the legacy
    path uses (``_apply_search_outcome`` and friends), so the shape is
    identical to the legacy response by construction.

    ``dashboard_leg`` carries the separately-served dashboards bucket for a
    ``dashboard``-including request (issue #2289); it is injected here, before
    the count / partial-mirror finalisation, so a ``fields=`` projection and
    the enforce-mode scrub both see the merged envelope.
    """
    response = _new_search_response(req.query, req.parsed_search_types)
    _emit_intent_skip_warning(response, req.body_skipped_by_intent_gate)

    if req.registry_eligible:
        parsed_result_fields = _parse_component_result_fields(req.result_fields)
        # Base records contain the six documented keys. Requested enrichment
        # fields retain the component's area/floor/labels/aliases registry join;
        # requested membership fields append its opt-in state-derived metadata.
        # With neither class requested, the default six-key shape is unchanged.
        record_keys = (
            *_ENTITY_RECORD_KEYS,
            *_requested_enrichment(parsed_result_fields),
            *_requested_membership(parsed_result_fields),
        )
        conditional_keys = set(_requested_membership(parsed_result_fields))
        entities = [
            {
                key: rec[key] if key in conditional_keys else rec.get(key)
                for key in record_keys
                if key not in conditional_keys or key in rec
            }
            for rec in _as_record_list(component_result.get("entities"))
        ]
        entity_has_more = bool(component_result.get("entity_has_more", False))
        entity_payload: dict[str, Any] = {
            "results": entities,
            "total_matches": int(
                component_result.get("entity_total_matches", len(entities)) or 0
            ),
            "has_more": entity_has_more,
            "next_offset": (req.offset + req.limit) if entity_has_more else None,
            "offset": req.offset,
            "limit": req.limit,
            "count": len(entities),
            "search_type": _component_entity_search_mode(req),
        }
        _component_listing_metadata(req, entity_payload, component_result)
        # Order mirrors _search_regular: group by domain first (it projects its
        # own records), then project the flat results[].
        _apply_by_domain_grouping(
            entity_payload,
            entities,
            req.group_by_domain,
            req.per_domain_limit,
            parsed_result_fields,
        )
        _apply_result_fields_to_response(entity_payload, parsed_result_fields)
        _apply_search_outcome(response, "entities", entity_payload)

    if req.body_eligible:
        _apply_search_outcome(
            response,
            "configs",
            _component_config_payload(
                req,
                component_result,
                dashboard_leg,
                preserve_core_fields=preserve_core_fields,
            ),
        )

    # The component reports a single overall partial flag (design § 1). In-process
    # joins are effectively never partial, but a body too large to serialize can
    # set it — carry it through honestly rather than assuming completeness.
    if component_result.get("partial"):
        response["partial"] = True
        reason = component_result.get("partial_reason")
        if isinstance(reason, str) and reason:
            _merge_partial_reason(response, reason)

    # The component also surfaces intentional per-surface diagnostics (e.g. a
    # config domain it couldn't read) separately from the overall partial flag.
    # A non-empty diagnostics map is a genuine incompleteness, so mark the
    # response partial and fold a readable clause into partial_reason rather than
    # dropping the component's signal.
    diagnostics = component_result.get("diagnostics")
    if isinstance(diagnostics, dict):
        diag_reason = _format_search_diagnostics(diagnostics)
        if diag_reason:
            response["partial"] = True
            _merge_partial_reason(response, diag_reason)

    if dashboard_leg is not None:
        _apply_dashboard_leg_state(response, dashboard_leg)

    _merge_component_visibility_warnings(response, component_result)

    response["count"] = len(response["entities"]) + sum(
        len(response.get(bucket, [])) for bucket in _CONFIG_BUCKETS
    )
    _synthesize_combined_pagination(response)
    _mirror_partial_to_warnings(response)
    return _project_response_fields(response, req.parsed_fields)
