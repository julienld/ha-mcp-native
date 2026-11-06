"""Qualify device writes, restore and the integration removal callback in Core."""

import asyncio
import time
from typing import Any


async def exercise_devices(checks: Any, base: str) -> None:
    created = await checks.call(
        "ha_set_integration",
        {"domain": "native_fixture", "config": {"name": "Native device"}},
    )
    assert created["success"], created
    deadline = time.monotonic() + 12
    while True:
        fixture = await checks.control("device")
        if fixture["device_id"]:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(fixture)
        await asyncio.sleep(0.1)
    device_id = fixture["device_id"]
    updated = await checks.call(
        "ha_set_device", {"device_id": device_id, "name": "Native user name"}
    )
    assert (
        updated["success"]
        and updated["device_entry"]["name_by_user"] == "Native user name"
    ), updated
    panel = base + "/api/ha_mcp_tools/ui/app/api/settings/backups"
    response = await checks.client.get(
        panel, params={"domain": "device", "entity_id": device_id}
    )
    response.raise_for_status()
    backups = response.json()["backups"]
    assert backups, response.json()
    restored = await checks.client.post(
        panel + "/" + backups[0]["name"] + "/restore", json={}
    )
    assert restored.status_code == 200 and restored.json()["success"], restored.text
    read = await checks.call("ha_get_device", {"device_id": device_id})
    assert (
        read["device"]["device_id"] == device_id
        and read["device"]["name_by_user"] is None
    ), read
    checks.passed("native_device_update_and_restore_preserve_Core_identity")
    removed = await checks.call("ha_remove_device", {"device_id": device_id})
    assert removed["success"], removed
    fixture = await checks.control("device")
    assert fixture["device_removals"] == 1, fixture
    listed = await checks.call("ha_get_device")
    assert all(row["device_id"] != device_id for row in listed["devices"]), listed
    checks.passed("native_device_removal_uses_real_integration_callback_once")
