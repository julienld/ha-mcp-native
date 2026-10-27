"""Reference-result merging and completeness disclosures shared by search."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


_ITEM_TYPE_TO_BUCKET: dict[str, str] = {
    "automation": "automations",
    "script": "scripts",
    "scene": "scenes",
}


_UNMODELLED_CONSUMER_TYPES: frozenset[str] = frozenset({"group", "person"})


_UP_RELATION_TYPES: frozenset[str] = frozenset(
    {"area", "device", "floor", "label", "config_entry", "integration", "entity"}
)


_KNOWN_ITEM_TYPES: frozenset[str] = (
    frozenset(_ITEM_TYPE_TO_BUCKET | dict.fromkeys(_UNMODELLED_CONSUMER_TYPES))
    | _UP_RELATION_TYPES
)


BUCKET_TO_ITEM_TYPE: dict[str, str] = {
    bucket: item_type for item_type, bucket in _ITEM_TYPE_TO_BUCKET.items()
}


GRAPH_ITEM_TYPES: frozenset[str] = frozenset(_ITEM_TYPE_TO_BUCKET)


@dataclass(frozen=True)
class GraphResult:
    """What Home Assistant's reference graph said about one entity.

    ``buckets`` maps a config-body bucket name to the entity_ids HA named.
    ``dropped`` maps an item type this server does not model (``group`` /
    ``person``) to the ids HA named for it. A caller that reports ``buckets``
    while ignoring a non-empty ``dropped`` must not describe its answer as a
    complete reference list.
    """

    buckets: dict[str, set[str]] = field(default_factory=dict)
    dropped: dict[str, set[str]] = field(default_factory=dict)


def _parse_related(payload: Any) -> GraphResult | None:
    """Map a ``search/related`` result onto buckets plus unmodelled consumers.

    HA's ``Searcher.async_search`` returns ``dict[ItemType, set[str]]``, which
    reaches the wire as a JSON object of arrays (its encoder coerces sets to
    lists). Sets are unordered, so nothing here may depend on element order —
    callers that render the result sort it.

    Returns ``None`` when the payload could not be understood at all: not a
    dict, or a dict naming no item type this code recognises. That case must
    NOT collapse into an empty success, because an empty success reads as
    "Home Assistant knows of no references" and licenses a completeness claim.
    A shape we cannot read is the opposite of evidence that nothing matched.

    A future item type we do not recognise, arriving ALONGSIDE ones we do, is
    ignored: the answer is still usable and mis-filing an unknown type into a
    bucket whose record shape it does not fit would be worse.
    """
    if not isinstance(payload, dict):
        return None
    result = GraphResult()
    understood = False
    for item_type, ids in payload.items():
        # Shape check FIRST: ``{"automation": "not-a-list"}`` must read as
        # unintelligible, not as an understood answer with nothing in it.
        if not isinstance(ids, list | set | tuple):
            continue
        if item_type not in _KNOWN_ITEM_TYPES:
            continue
        entity_ids = {i for i in ids if isinstance(i, str) and i}
        if ids and not entity_ids:
            # A bucket Home Assistant populated but whose elements this code
            # cannot read (a shape change upstream, say). Dropping them while
            # reporting success would let the response claim every plain
            # reference was counted on the strength of a bucket it discarded.
            logger.debug(
                "search/related bucket %r held %d unreadable element(s)",
                item_type,
                len(ids),
            )
            return None
        understood = True
        if not entity_ids:
            continue
        if bucket := _ITEM_TYPE_TO_BUCKET.get(item_type):
            result.buckets[bucket] = entity_ids
        elif item_type in _UNMODELLED_CONSUMER_TYPES:
            result.dropped[item_type] = entity_ids
    # An empty dict is a legitimate answer (HA filters empty sets out), so it
    # counts as understood; a populated dict naming nothing we know does not.
    if not understood and payload:
        return None
    return result


_GRAPH_HIT_SCORE = 100


def _graph_record(
    bucket: str,
    entity_id: str,
    friendly_name: str | None,
    resolve_scene_id: Callable[[str], str | None],
) -> dict[str, Any]:
    """Build one bucket record for an entity the reference graph flagged.

    ``config`` is ``None``: a graph hit is reported whether or not its body was
    ever fetched, and the whole point is that it may be unfetchable (YAML-
    defined) or deprioritized past the budget. The per-bucket id fields mirror
    what the body-scan builders emit so a caller cannot tell the two apart by
    shape.

    ``resolve_scene_id`` is the scene branch's own storage-key resolver,
    injected rather than reimplemented. A scene's storage key is its registry
    ``unique_id``, which diverges from the entity slug for any scene renamed in
    the UI, and it is the key ``ha_config_get_scene`` /
    ``ha_config_delete_scene`` take. Reusing the resolver keeps one tier order
    and, when it does fall back to the slug, keeps the warning that makes the
    unresolvable case visible (#1168).
    """
    record: dict[str, Any] = {
        "entity_id": entity_id,
        "friendly_name": friendly_name or entity_id,
        "score": _GRAPH_HIT_SCORE,
        "match_in_name": False,
        "match_in_config": False,
        "match_in_references": True,
        "config": None,
    }
    if bucket == "scripts":
        record["script_id"] = entity_id.removeprefix("script.")
    elif bucket == "scenes":
        record["scene_id"] = resolve_scene_id(entity_id.removeprefix("scene."))
    return record


def _merge_graph_hits(
    results: dict[str, list[dict[str, Any]]],
    related: GraphResult | None,
    all_entities: list[dict[str, Any]],
    search_types: list[str],
    resolve_scene_id: Callable[[str], str | None],
) -> set[str]:
    """Fold reference-graph hits into ``results``, in place.

    ``related`` is ``None`` when the graph was not consulted or could not be
    read, and carries empty buckets when HA knows of no references; both are a
    no-op here, so the caller needs no guard of its own.

    Returns the bucket names HA named but this call excluded via
    ``search_types``, so the caller can disclose them.

    A config found BOTH ways stays one record carrying both flags — reporting
    it twice would double-count it in ``total_matches`` and read as two
    distinct consumers. Buckets the caller did not ask for are skipped, so a
    ``search_types``-pinned call never grows a surface it excluded.

    Runs before ``_scrub_results_for_enforce`` and before pagination so graph
    hits get identical visibility-enforcement treatment and paginate as
    ordinary records, rather than needing a parallel path for either.

    Ids arrive from a JSON array decoded out of HA's unordered ``set``, so they
    are sorted here: without it the tail of an over-limit result would shuffle
    between identical calls.
    """
    if related is None or not related.buckets:
        # Nothing to merge, so skip building a name map over the whole state
        # machine. ``skipped`` is derived from ``buckets`` alone, and
        # ``dropped`` is disclosed by the partial message, not by this merge.
        return set()
    skipped: set[str] = set()
    name_by_entity_id = {
        entity.get("entity_id"): entity.get("attributes", {}).get("friendly_name")
        for entity in all_entities
    }
    for bucket, entity_ids in related.buckets.items():
        if BUCKET_TO_ITEM_TYPE[bucket] not in search_types:
            # HA named references on a surface this call excluded. Recorded so
            # the response can say so rather than imply the list is complete.
            skipped.add(bucket)
            continue
        records = results.setdefault(bucket, [])
        by_entity_id = {r.get("entity_id"): r for r in records}
        for entity_id in sorted(entity_ids):
            existing = by_entity_id.get(entity_id)
            if existing is not None:
                existing["match_in_references"] = True
                continue
            records.append(
                _graph_record(
                    bucket,
                    entity_id,
                    name_by_entity_id.get(entity_id),
                    resolve_scene_id,
                )
            )
    return skipped


def _graph_partial_reasons(
    *,
    graph_consulted: bool,
    graph_dropped: dict[str, set[str]] | None,
    graph_surfaces_skipped: set[str] | None,
    graph_unavailable: bool,
    body_scan_incomplete: bool,
) -> list[str]:
    """Reference-graph fragments for ``partial_reason``.

    Three distinct disclosures, each of which would otherwise let a caller read
    a reference list as complete when it is not:

    - The graph could not be consulted at all on a query it applies to, so the
      answer rests on the config-body scan alone.
    - Home Assistant named consumers this server does not model (``group`` /
      ``person``). They break on a rename exactly like an automation does.
    - Home Assistant named references on a surface ``search_types`` excluded.

    The scope sentence is deliberately narrow. It claims only what the graph
    actually covers: automations, scripts and scenes, among the surfaces this
    call searched, counted into ``total_matches`` rather than "listed above"
    (pagination renders one page). Helper bodies and dashboard cards are not
    graph item types, so a plain reference from one is never covered.

    It also excludes anything an entity-visibility filter concealed, because
    ``_scrub_results_for_enforce`` runs AFTER the graph merge and can drop a
    record this sentence would otherwise vouch for. The caveat is stated
    unconditionally, not only when enforce mode is on: phrased as a category
    it reveals nothing about whether anything was actually concealed, whereas
    adding it only when the filter is active would itself signal that it is.
    Naming a count here would leak what enforce mode exists to conceal --
    collection reads omit silently by design (docs/FAQ.md), and a list makes
    no completeness claim, which is precisely what this sentence must not
    turn it into.
    """
    reasons: list[str] = []
    if graph_unavailable:
        reasons.append(
            "Home Assistant's reference graph could not be consulted for this "
            "query, so these results come from the configuration-body scan "
            "alone; a reference inside a config that could not be read is not "
            "represented here."
        )
    for item_type, ids in sorted((graph_dropped or {}).items()):
        if not ids:
            continue
        # Count only, never the ids. Under enforce mode the outbound scan
        # refuses any response carrying a hidden entity_id, so naming them here
        # would fail the whole search AND leak what that mode conceals; a
        # collection read omits silently by design. The count still tells the
        # caller its reference list is not the whole story.
        reasons.append(
            f"Home Assistant also reports {len(ids)} {item_type}(s) "
            f"referencing this entity. ha_search does not model that surface, "
            f"so they are counted here rather than listed as matches; they "
            f"break on a rename like any other reference. Home Assistant's own "
            f"Related view lists them."
        )
    if graph_surfaces_skipped:
        surfaces = ", ".join(sorted(graph_surfaces_skipped))
        reasons.append(
            f"Home Assistant also reports references in {surfaces}, which this "
            "call excluded via `search_types`; re-run without that filter to "
            "see them."
        )
    if graph_consulted and body_scan_incomplete:
        reasons.append(
            "Scope of that unknown: Home Assistant's reference graph was "
            "consulted, so every plain (non-templated) reference from an "
            "automation, script or scene among the surfaces this call searched "
            "is counted in total_matches, including from configs that were "
            "never read, except any record an entity-visibility filter "
            "conceals from you. What an unread config can still hide is a "
            "TEMPLATED reference such as {{ states('...') }}, which the graph "
            "cannot see, and a reference from a helper body or dashboard card, "
            "which are not graph item types."
        )
    return reasons
