"""Project a detached search image without accessing live Core collections."""

import json
from dataclasses import replace
from typing import Any

from ha_mcp.runtime.search_component_results import (
    _DashboardLeg,
    _merge_dashboard_window,
    _shape_component_search_response,
)
from ha_mcp.runtime.search_response import _ResolvedSearch
from ha_mcp.runtime.search_scoring import ConfigurationScoring

from ..core_secrets import _scrub_secret_values
from .search_matching import score_candidates, visible_record


def project_search(
    snapshot: bytes, req: _ResolvedSearch, threshold: int
) -> dict[str, Any]:
    image = json.loads(snapshot)
    matcher = score_candidates(image, req, threshold)
    raw = image["result"]
    dashboards = image["dashboards"]
    scorer = ConfigurationScoring()
    matches = []
    for document in dashboards["records"]:
        if not visible_record(document, matcher, image["enforce"]):
            continue
        body = _scrub_secret_values(
            document["config"], frozenset(image["secret_values"])
        )
        score = scorer._search_in_dict(body, req.query_text.lower(), req.exact_match)
        if score < (100 if req.exact_match else threshold):
            continue
        record = {
            "url_path": document["url_path"],
            "title": document["title"],
            "source": document["mode"],
            "score": score,
            "match_in_config": True,
            "match_in_references": False,
        }
        if req.include_config:
            record["config"] = body if document["mode"] == "storage" else None
        matches.append(record)
    raw, page = _merge_dashboard_window(req, raw, matches)
    if req.include_config:
        for bucket in ("automations", "scripts", "scenes", "helpers"):
            for row in raw[bucket]:
                key = row.get("entity_id") or row.get("entry_id")
                body = image["bodies"].get(key)
                row["config"] = _scrub_secret_values(
                    body, frozenset(image["secret_values"])
                )
                if key in image["body_omissions"]:
                    raw["partial"] = True
                    raw["partial_reason"] = " ; ".join(
                        part
                        for part in (
                            raw.get("partial_reason"),
                            "Configuration body omitted (too large)",
                        )
                        if part
                    )
    leg = (
        _DashboardLeg(page, failed=dashboards["failed"], error=dashboards["error"])
        if image["dashboards_requested"]
        else None
    )
    # Project only after references and dashboards have joined the same window.
    return _shape_component_search_response(
        replace(req), raw, dashboard_leg=leg, preserve_core_fields=True
    )
