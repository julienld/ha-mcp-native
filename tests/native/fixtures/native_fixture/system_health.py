"""A real Core health platform with immediate and asynchronously resolved values."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.system_health import SystemHealthRegistration
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

_STAMP = datetime(2026, 10, 1, tzinfo=UTC)


@callback
def async_register(hass: HomeAssistant, register: SystemHealthRegistration) -> None:
    register.async_register_info(_health_info, manage_url="/config/native_fixture")


async def _resolved_date() -> datetime:
    await asyncio.sleep(0)
    return _STAMP


async def _health_info(hass: HomeAssistant) -> dict[str, Any]:
    return {"fixture_ready": True, "completed_at": _resolved_date()}


async def seed_health(hass: HomeAssistant) -> dict[str, Any]:
    issue_id = "native_health_qualification"
    ir.async_create_issue(
        hass,
        "native_fixture",
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=issue_id,
    )
    await hass.async_block_till_done()
    entry_id = hass.data["native_fixture"]["sensor_entry_id"]
    assert hass.config_entries.async_get_entry(entry_id) is not None
    return {
        "issue_id": issue_id,
        "entry_id": entry_id,
        "completed_at": _STAMP.isoformat(),
    }
