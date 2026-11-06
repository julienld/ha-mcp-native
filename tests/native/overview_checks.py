"""Compare the native overview's aggregates with Core's actual state image."""

from typing import Any


async def exercise_overview(checks: Any, base: str) -> None:
    response = await checks.client.get(base + "/api/states", headers=checks.headers())
    response.raise_for_status()
    states = response.json()
    sensors = [state for state in states if state["entity_id"].startswith("sensor.")]
    assert sensors, states
    result = await checks.call(
        "ha_get_overview",
        {"detail_level": "full", "domains": ["sensor"], "limit": 1},
    )
    assert result["success"], result
    assert result["system_summary"]["total_entities"] == len(states), result
    assert result["domain_stats"]["sensor"]["count"] == len(sensors), result
    assert result["pagination"]["entities_returned"] == 1, result
    assert len(result["domain_stats"]["sensor"]["entities"]) == 1, result
    assert "sensor" in result["system_info"]["components"], result
    assert result.get("settings_url", "").endswith("/api/ha_mcp_tools/ui/boot") or (
        "/api/ha_mcp_tools/ui/boot" in result.get("settings_url_hint", "")
    ), result
    checks.passed(
        "native_overview_Core_state_aggregates_pagination_and_HA_settings_URL"
    )

    info = await checks.call("ha_get_overview", {"fields": ["system_info"]})
    assert info["success"] and info["system_info"]["version"], info
    assert "domain_stats" not in info and "system_summary" not in info, info
    checks.passed("native_overview_independent_Core_information_projection")
