"""Score only detached Core candidates, after the visibility and secret filters."""

from typing import Any

from ha_mcp.runtime.search_references import (
    GraphResult,
    _graph_partial_reasons,
    _merge_graph_hits,
)
from ha_mcp.runtime.search_response import _CONFIG_BUCKETS, _ResolvedSearch
from ha_mcp.visibility.enforcement import _HiddenMatcher, _scan_value

from ..core_secrets import _scrub_secret_values
from ..websocket_api.search import _entity_match_type
from ..websocket_api.search_score import (
    _apply_hidden_penalty,
    _config_score,
    _text_tier,
)


def visible_record(row: dict[str, Any], matcher: _HiddenMatcher, enforce: bool) -> bool:
    if row.get("entity_id") in matcher.hidden:
        return False
    if not enforce:
        return True
    exact, embedded = _scan_value(row, matcher)
    return exact is None and not embedded


def _match_entities(
    image: dict[str, Any],
    req: _ResolvedSearch,
    threshold: int,
    matcher: _HiddenMatcher,
) -> None:
    rows = []
    for row in image["result"]["entities"]:
        corpus = image["entity_corpora"][row["entity_id"]]
        if not visible_record(corpus, matcher, image["enforce"]):
            continue
        if req.query_text:
            score = _text_tier(
                req.query_text.lower(),
                corpus["_match_texts"],
                fuzzy=not req.exact_match,
                threshold=threshold,
            )
            if score is None:
                continue
            row["score"] = _apply_hidden_penalty(score, corpus["_hidden"])
            row["match_type"] = _entity_match_type(
                req.query_text.lower(),
                row["entity_id"],
                row["friendly_name"],
                row["domain"],
                row["aliases"],
                exact=req.exact_match,
            )
        rows.append(row)
    rows.sort(key=lambda row: (-row["score"], row["entity_id"]))
    image["result"]["entities"] = rows
    image["result"]["entity_total_matches"] = len(rows)
    image["result"]["entity_has_more"] = False


def _match_configs(
    image: dict[str, Any],
    req: _ResolvedSearch,
    threshold: int,
    matcher: _HiddenMatcher,
) -> None:
    secrets = frozenset(image["secret_values"])
    for bucket in ("automations", "scripts", "scenes", "helpers"):
        matches = []
        for row in image["result"][bucket]:
            key = row.get("entity_id") or row.get("entry_id")
            body = image["corpora"].get(key)
            if not visible_record({**row, "config": body}, matcher, image["enforce"]):
                continue
            name = row.get("alias") or row.get("name") or row.get("friendly_name") or ""
            scored = _config_score(
                req.query_text.lower(),
                row.get("entity_id") or name,
                name,
                _scrub_secret_values(body, secrets),
                exact=req.exact_match,
                secret_values=secrets,
                fuzzy_threshold=threshold,
            )
            if scored is None:
                continue
            row["score"], row["match_in_name"], row["match_in_config"] = scored
            matches.append(row)
        image["result"][bucket] = matches


def _merge_references(
    image: dict[str, Any], req: _ResolvedSearch, matcher: _HiddenMatcher
) -> list[str]:
    graph = image["graph"]
    related = None
    if graph is not None:
        related = GraphResult(
            {
                bucket: {
                    eid
                    for eid in ids
                    if (not image["enforce"] or image["corpora"].get(eid) is not None)
                    and visible_record(
                        {"entity_id": eid, "config": image["corpora"].get(eid)},
                        matcher,
                        image["enforce"],
                    )
                }
                for bucket, ids in graph["buckets"].items()
            },
            {
                kind: {
                    eid
                    for eid in ids
                    if visible_record({"entity_id": eid}, matcher, image["enforce"])
                }
                for kind, ids in graph["dropped"].items()
            },
        )
    skipped = _merge_graph_hits(
        image["result"],
        related,
        image["graph_states"],
        req.parsed_search_types or ["automation", "script", "scene", "helper"],
        lambda slug: image["graph_identities"].get(f"scene.{slug}", slug),
    )
    for bucket in ("automations", "scripts", "scenes"):
        for row in image["result"][bucket]:
            if "id" not in row and row["entity_id"] in image["graph_identities"]:
                row["id"] = image["graph_identities"][row["entity_id"]]
    return _graph_partial_reasons(
        graph_consulted=image["graph_consulted"] and related is not None,
        graph_dropped=related.dropped if related is not None else None,
        graph_surfaces_skipped=skipped,
        graph_unavailable=image["graph_consulted"] and related is None,
        body_scan_incomplete=bool(image["source_reasons"]),
    )


def score_candidates(
    image: dict[str, Any], req: _ResolvedSearch, threshold: int
) -> _HiddenMatcher:
    matcher = _HiddenMatcher.from_hidden(set(image["hidden"]))
    _match_entities(image, req, threshold, matcher)
    _match_configs(image, req, threshold, matcher)
    reasons = [*image["source_reasons"], *_merge_references(image, req, matcher)]
    raw = image["result"]
    for bucket in ("automations", "scripts", "scenes", "helpers"):
        # Reference-only hits acquire metadata from Core after text scoring;
        # apply the same complete-record barrier before their totals too.
        raw[bucket] = [
            row
            for row in raw[bucket]
            if visible_record(
                {
                    **row,
                    "config": image["corpora"].get(
                        row.get("entity_id") or row.get("entry_id")
                    ),
                },
                matcher,
                image["enforce"],
            )
        ]
    if reasons:
        raw["partial"] = True
        raw["partial_reason"] = " ; ".join(
            part for part in (raw.get("partial_reason"), *reasons) if part
        )
    raw["config_total_matches"] = sum(
        len(raw.get(bucket, [])) for bucket in _CONFIG_BUCKETS
    )
    raw["config_has_more"] = False
    return matcher
