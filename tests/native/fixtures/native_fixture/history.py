"""Create disposable, committed recorder rows through Core's native APIs."""

from datetime import timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter


async def seed_history(hass: HomeAssistant) -> dict[str, Any]:
    """Return exact test bounds only after both state and statistics commits."""
    recorder = get_instance(hass)
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=3
    )
    reset = start - timedelta(days=1)
    statistic_id = "native_fixture:energy"
    metadata: StatisticMetaData = {
        "statistic_id": statistic_id,
        "source": "native_fixture",
        "name": "Native recorder fixture energy",
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "unit_class": EnergyConverter.UNIT_CLASS,
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }
    statistics: list[StatisticData] = [
        {"start": start, "state": 1.0, "sum": 10.0, "last_reset": reset},
        {
            "start": start + timedelta(hours=1),
            "state": 3.0,
            "sum": 12.0,
            "last_reset": reset,
        },
    ]
    async_add_external_statistics(hass, metadata, statistics)
    await recorder.async_block_till_done()

    entity_id = "sensor.native_history"
    history_start = dt_util.utcnow() - timedelta(seconds=1)
    attributes = {
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": "energy",
        "state_class": "total_increasing",
        "friendly_name": "Native recorder fixture sensor",
    }
    for value in ("1", "2"):
        hass.states.async_set(entity_id, value, attributes)
        await hass.async_block_till_done()
        await recorder.async_block_till_done()
    return {
        "statistic_id": statistic_id,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(),
        "last_reset_ms": reset.timestamp() * 1000,
        "state_entity_id": entity_id,
        "state_start": history_start.isoformat(),
    }
