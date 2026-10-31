"""Emit disposable events through the real system-log and logbook owners."""

from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.core import HomeAssistant


async def seed_logs(hass: HomeAssistant) -> dict[str, Any]:
    marker = "native_fixture_logs_qualification"
    for _ in range(2):
        await hass.services.async_call(
            "system_log",
            "write",
            {"message": marker, "level": "warning", "logger": "native_fixture.logs"},
            blocking=True,
        )
    await hass.services.async_call(
        "logbook",
        "log",
        {"name": "Native log fixture", "message": marker, "domain": "native_fixture"},
        blocking=True,
    )
    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    return {"marker": marker}
