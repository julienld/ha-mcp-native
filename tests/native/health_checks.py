"""Qualify actual Core health platforms, repairs, config and diagnostics."""

from typing import Any


async def exercise_health(checks: Any) -> None:
    seeded = await checks.control("seed_health", {})
    result = await checks.call(
        "ha_get_system_health", {"include": "repairs,themes,config_check,dead_entities"}
    )
    envelope = result["health_info"]
    assert envelope["type"] == "initial", result
    domains = envelope["data"]
    assert result["component_count"] == len(domains) and len(domains) > 0, result
    fixture = domains["native_fixture"]
    assert fixture["manage_url"] == "/config/native_fixture", fixture
    assert fixture["info"]["fixture_ready"] is True, fixture
    assert fixture["info"]["completed_at"] == {
        "type": "date",
        "value": seeded["completed_at"],
    }, fixture
    checks.passed("native_health_resolves_registered_Core_values_and_counts_domains")

    assert any(
        row["domain"] == "native_fixture" and row["issue_id"] == seeded["issue_id"]
        for row in result["repairs"]["issues"]
    ), result
    assert result["config_check"]["is_valid"] is True, result["config_check"]
    assert "error" not in result["themes"], result["themes"]
    assert result["dead_entities"]["config_entries_checked"] is True, result
    assert "ha_mcp_update" not in result, result
    checks.passed(
        "native_health_reads_Core_repairs_themes_config_and_registry_snapshot"
    )

    diagnostic = await checks.call(
        "ha_get_system_health",
        {
            "include": "diagnostics",
            "config_entry_id": seeded["entry_id"],
            "diagnostics_fields": ["data"],
            "diagnostics_data_path": "data.values",
            "diagnostics_data_offset": 1,
            "diagnostics_data_limit": 1,
        },
    )
    page = diagnostic["diagnostics"]["data"]
    assert page["items"] == [{"id": 2}] and page["total"] == 3, diagnostic
    assert diagnostic["diagnostics"]["config_entry_id"] == seeded["entry_id"], (
        diagnostic
    )
    checks.passed("native_health_projects_real_Core_diagnostics_with_pagination")
