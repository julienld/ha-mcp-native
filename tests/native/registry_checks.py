"""Validate registry writes and their recovery points on the actual Core owners."""

from typing import Any


async def exercise_registries(checks: Any, base: str) -> None:
    floor = await checks.call(
        "ha_set_area_or_floor",
        {"kind": "floor", "name": "Native registry floor", "level": 1},
    )
    assert floor["success"], floor
    floor_id = floor["floor_id"]
    area = await checks.call(
        "ha_set_area_or_floor",
        {"kind": "area", "name": "Native registry area", "floor_id": floor_id},
    )
    assert area["success"], area
    area_id = area["area_id"]
    updated = await checks.call(
        "ha_set_area_or_floor",
        {"kind": "area", "id": area_id, "aliases": ["Native alias"]},
    )
    assert updated["success"], updated
    locations = await checks.call("ha_list_floors_areas")
    selected = next(row for row in locations["floors"] if row["floor_id"] == floor_id)
    stored = next(row for row in selected["areas"] if row["area_id"] == area_id)
    assert stored["aliases"] == ["Native alias"], locations
    checks.passed("native_area_and_floor_writes_use_live_Core_registries")

    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    response = await checks.client.get(
        panel, params={"domain": "area_or_floor", "entity_id": "area:" + area_id}
    )
    response.raise_for_status()
    backups = response.json()["backups"]
    assert backups, response.json()
    restored = await checks.client.post(
        panel + "/" + backups[0]["name"] + "/restore", json={}
    )
    assert restored.status_code == 200, restored.text
    locations = await checks.call("ha_list_floors_areas")
    selected = next(row for row in locations["floors"] if row["floor_id"] == floor_id)
    stored = next(row for row in selected["areas"] if row["area_id"] == area_id)
    assert stored["aliases"] == [] and stored["floor_id"] == floor_id, locations
    checks.passed("native_area_restore_retains_original_floor_and_prewrite_fields")

    category = await checks.call(
        "ha_config_set_category", {"scope": "script", "name": "Native category"}
    )
    assert category["success"], category
    category_id = category["category_id"]
    updated = await checks.call(
        "ha_config_set_category",
        {
            "scope": "script",
            "name": "Native category updated",
            "category_id": category_id,
        },
    )
    assert updated["success"], updated
    read = await checks.call(
        "ha_config_get_category", {"scope": "script", "category_id": category_id}
    )
    assert read["category"]["name"] == "Native category updated", read
    deleted = await checks.call(
        "ha_config_remove_category", {"scope": "script", "category_id": category_id}
    )
    assert deleted["success"], deleted
    checks.passed("native_scoped_category_create_update_read_and_remove")

    for kind, identifier in (("area", area_id), ("floor", floor_id)):
        result = await checks.call(
            "ha_remove_area_or_floor", {"kind": kind, "id": identifier}
        )
        assert result["success"], result
