"""Qualify entity settings and all three history meanings against real Core."""

import asyncio
import time
from typing import Any


async def _wait_sensor(checks: Any, *, disabled: bool = False) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while True:
        fixture: dict[str, Any] = await checks.control("entity")
        row = fixture.get("registry_entry")
        if row is not None and (
            (row["disabled_by"] == "user" and fixture["state"] is None)
            if disabled
            else fixture["state"] is not None
        ):
            return fixture
        if time.monotonic() >= deadline:
            raise AssertionError(fixture)
        await asyncio.sleep(0.1)


async def _backups(checks: Any, panel: str, domain: str, identity: str) -> list[dict]:
    response = await checks.client.get(
        panel, params={"domain": domain, "entity_id": identity}
    )
    response.raise_for_status()
    rows: list[dict] = response.json()["backups"]
    assert rows, response.json()
    return rows


async def _restore(checks: Any, panel: str, snapshot: str) -> dict[str, Any]:
    response = await checks.client.post(panel + "/" + snapshot + "/restore", json={})
    assert response.status_code == 200 and response.json()["success"], response.text
    result: dict[str, Any] = response.json()
    return result


async def exercise_entities(checks: Any, base: str) -> None:
    created = await checks.call(
        "ha_set_integration",
        {"domain": "native_fixture", "config": {"name": "Native entity device"}},
    )
    assert created["success"], created
    fixture = await _wait_sensor(checks)
    before = fixture["registry_entry"]
    entity_id, identity = fixture["entity_id"], fixture["registry_id"]
    assert before["platform"] == "native_fixture" and before["device_id"], fixture
    renamed = entity_id + "_renamed"
    result = await checks.call(
        "ha_set_entity",
        {
            "entity_id": entity_id,
            "new_entity_id": renamed,
            "name": "Native entity settings",
            "icon": "mdi:flash",
            "hidden": True,
            "aliases": ["Native entity alias"],
            "options": {"sensor": {"display_precision": 2}},
        },
    )
    assert result["success"] and result["entity_id"] == renamed, result
    fixture = await checks.control("entity")
    row = fixture["registry_entry"]
    assert fixture["registry_id"] == identity and row["entity_id"] == renamed, fixture
    assert row["name"] == "Native entity settings" and row["icon"] == "mdi:flash", (
        fixture
    )
    assert row["hidden_by"] == "user" and "Native entity alias" in row["aliases"], (
        fixture
    )
    assert row["options"]["sensor"]["display_precision"] == 2, fixture
    checks.passed(
        "native_SensorEntity_settings_options_and_rename_use_real_Core_registry"
    )

    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    snapshot = (await _backups(checks, panel, "entity_registry", identity))[0]["name"]
    diff = await checks.client.get(panel + "/" + snapshot + "/diff")
    assert diff.status_code == 200 and "Native entity settings" in diff.text, diff.text
    await _restore(checks, panel, snapshot)
    fixture = await checks.control("entity")
    restored = fixture["registry_entry"]
    assert fixture["registry_id"] == identity and restored["entity_id"] == entity_id, (
        fixture
    )
    for key in (
        "name",
        "icon",
        "hidden_by",
        "aliases",
        "options",
        "unique_id",
        "config_entry_id",
    ):
        assert restored[key] == before[key], (key, before, restored)
    checks.passed("native_entity_registry_diff_and_restore_follow_UUID_across_rename")

    await _exercise_state_history(checks, panel)

    disabled = await checks.call(
        "ha_set_entity", {"entity_id": entity_id, "enabled": False}
    )
    assert disabled["success"], disabled
    await _wait_sensor(checks, disabled=True)
    removed = await checks.call("ha_remove_entity", {"entity_id": entity_id})
    assert removed["success"], removed
    fixture = await checks.control("entity")
    assert fixture["entity_id"] is None and fixture["registry_entry"] is None, fixture
    checks.passed("native_disabled_SensorEntity_removal_uses_real_Core_registration")


async def _exercise_state_history(checks: Any, panel: str) -> None:
    seeded = await checks.control("seed_entity_history", {})
    state_id = seeded["entity_id"]
    exposure_id = seeded["exposure_entity_id"]
    fixture = await checks.control("entity")
    assert fixture["legacy_preferences"] is None, fixture
    assert fixture["legacy_exposure_entity_id"] == exposure_id, fixture
    assert fixture["legacy_state"]["entity_id"] == state_id, fixture
    assert (
        not fixture["legacy_exposure_has_state"]
        and not fixture["legacy_exposure_has_registration"]
    ), fixture
    exposed = await checks.call(
        "ha_set_entity",
        {"entity_id": exposure_id, "expose_to": {"conversation": False}},
    )
    assert exposed["success"], exposed
    fixture = await checks.control("entity")
    assert (
        fixture["legacy_preferences"]["assistants"]["conversation"]["should_expose"]
        is False
    ), fixture
    assert (
        not fixture["legacy_exposure_has_state"]
        and not fixture["legacy_exposure_has_registration"]
    ), fixture
    snapshot = (await _backups(checks, panel, "entity_exposure", exposure_id))[0][
        "name"
    ]
    await _restore(checks, panel, snapshot)
    fixture = await checks.control("entity")
    assert fixture["legacy_preferences"] is None, fixture
    assert (
        not fixture["legacy_exposure_has_state"]
        and not fixture["legacy_exposure_has_registration"]
    ), fixture
    checks.passed(
        "native_legacy_exposure_store_restores_absence_versus_false_without_registration"
    )

    diff = await checks.client.get(panel + "/" + seeded["snapshot"] + "/diff")
    assert diff.status_code == 200 and "changed" in diff.text, diff.text
    result = await _restore(checks, panel, seeded["snapshot"])
    fixture = await checks.control("entity")
    assert fixture["legacy_state"]["state"] == "saved", fixture
    assert fixture["legacy_state"]["attributes"]["fixture"] == "before", fixture
    assert fixture["legacy_state"]["context"]["user_id"], fixture
    assert "physical device" in str(result) and "automations" in str(result), result
    checks.passed("native_historical_entity_restore_changes_only_StateMachine_values")

    rejected = await checks.client.post(
        panel + "/" + seeded["invalid_snapshot"] + "/restore", json={}
    )
    assert rejected.status_code == 409 and "not_applied" in rejected.text, rejected.text
    fixture = await checks.control("entity")
    assert fixture["legacy_state"]["state"] == "saved", fixture
    checks.passed("native_state_history_rejects_invalid_Core_state_before_dispatch")
