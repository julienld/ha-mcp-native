"""Qualify Search against loaded Core entities, config views and Lovelace owners."""

import asyncio
import time
from typing import Any
from uuid import uuid4


async def _wait_match(
    checks: Any, query: str, surface: str, expected: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while True:
        result = await checks.call(
            "ha_search",
            {
                "query": query,
                "search_types": [surface],
                "include_config": True,
                "limit": 100,
                "exact_match": True,
            },
        )
        bucket = "dashboards" if surface == "dashboard" else surface + "s"
        matches = [
            row
            for row in result[bucket]
            if expected
            in (row.get("entity_id"), row.get("url_path"), row.get("scene_id"))
        ]
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            raise AssertionError(result)
        await asyncio.sleep(0.1)


async def exercise_search(checks: Any, common: dict[str, Any], base: str) -> None:
    response = await checks.client.get(base + "/api/states", headers=checks.headers())
    response.raise_for_status()
    sensors = [row for row in response.json() if row["entity_id"].startswith("sensor.")]
    listed = await checks.call("ha_search", {"domain_filter": "sensor", "limit": 1})
    assert listed["success"] and listed["entity_total_matches"] == len(sensors), listed
    assert len(listed["entities"]) == 1 and listed["entities"][0][
        "entity_id"
    ].startswith("sensor."), listed
    checks.passed("native_search_Core_registry_listing_counts_and_window")

    suffix = uuid4().hex[:10]
    identifier = "native_search_" + suffix
    marker = "native_search_marker_" + suffix
    dashboard = "native-search-" + suffix
    created = await checks.call(
        "ha_config_set_script",
        {
            **common,
            "script_id": identifier,
            "config": {
                "alias": "Native Search script",
                "sequence": [
                    {"variables": {"native_search_marker": marker}},
                    {
                        "action": "input_boolean.turn_on",
                        "target": {"entity_id": "input_boolean.native_light"},
                    },
                ],
            },
        },
    )
    assert created["success"], created
    script = await _wait_match(checks, marker, "script", "script." + identifier)
    assert (
        script["config"]["sequence"][0]["variables"]["native_search_marker"] == marker
    ), script
    assert script["source"] == "storage" and script["script_id"] == identifier, script
    references = await checks.call(
        "ha_search",
        {
            "query": "input_boolean.native_light",
            "search_types": ["script"],
            "limit": 100,
        },
    )
    graph_hit = next(
        row
        for row in references["scripts"]
        if row["entity_id"] == "script." + identifier
    )
    assert graph_hit["match_in_references"] is True, references
    checks.passed("native_search_registered_ConfigEditView_and_real_Core_Searcher")

    scene = await checks.call(
        "ha_config_set_scene",
        {
            **common,
            "scene_id": identifier,
            "config": {
                "name": "Native Search scene",
                "entities": {
                    "input_boolean.native_light": {
                        "state": "off",
                        "friendly_name": marker,
                    }
                },
            },
        },
    )
    assert scene["success"], scene
    scene_match = await _wait_match(checks, marker, "scene", identifier)
    assert (
        scene_match["config"]["entities"]["input_boolean.native_light"]["friendly_name"]
        == marker
    ), scene_match
    checks.passed("native_search_scene_matches_persisted_values_not_runtime_State_repr")

    saved = await checks.call(
        "ha_config_set_dashboard",
        {
            **common,
            "url_path": dashboard,
            "title": "Native Search dashboard",
            "config": {"views": [{"path": "home", "title": marker, "cards": []}]},
        },
    )
    assert saved["success"], saved
    await _wait_match(checks, marker, "dashboard", dashboard)
    page = await checks.call(
        "ha_search",
        {
            "query": marker,
            "search_types": ["script", "scene", "dashboard"],
            "limit": 1,
            "offset": 1,
        },
    )
    assert page["count"] == 1 and page["config_total_matches"] == 3, page
    assert page["config_next_offset"] == 2, page
    checks.passed("native_search_dashboard_and_config_share_one_counted_window")

    yaml = await _wait_match(
        checks, "native_yaml_probe", "script", "script.native_search_yaml"
    )
    assert yaml["source"] == "yaml" and yaml["config"] is None, yaml
    assert yaml["match_in_config"] is True, yaml
    for value, exact in (
        ("native-fixture-secret-probe", True),
        ("91827364", True),
        ("91827364", False),
    ):
        probe = await checks.call(
            "ha_search",
            {"query": value, "search_types": ["script"], "exact_match": exact},
        )
        assert probe["config_total_matches"] == 0, probe
    yaml_dashboard = await _wait_match(
        checks, "native_yaml_dashboard_probe", "dashboard", "native-yaml-search"
    )
    assert yaml_dashboard["config"] is None and yaml_dashboard["source"] == "yaml", (
        yaml_dashboard
    )
    assert yaml_dashboard["title"] == "Native YAML Search", yaml_dashboard
    checks.passed(
        "native_search_real_YAML_matching_withheld_bodies_and_secret_probe_barrier"
    )

    await checks.call("ha_config_delete_dashboard", {"url_path": dashboard})
    await checks.call("ha_config_remove_scene", {"scene_id": identifier})
    await checks.call("ha_config_remove_script", {"script_id": identifier})
