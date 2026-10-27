"""Observe and seed actual Lovelace owners without replacing their behavior."""

from typing import Any

from homeassistant.components.lovelace.const import LOVELACE_DATA, ConfigNotFound
from homeassistant.components.lovelace.dashboard import LovelaceStorage
from homeassistant.core import HomeAssistant


async def dashboard_probe(hass: HomeAssistant) -> dict[str, Any]:
    data = hass.data[LOVELACE_DATA]
    rows = []
    for key, owner in data.dashboards.items():
        try:
            config = await owner.async_load(False)
        except ConfigNotFound:
            config = None
        rows.append(
            {
                "key": key,
                "owner_type": type(owner).__name__,
                "metadata": owner.config,
                "config": config,
            }
        )
    return {"resources_loaded": data.resources.loaded, "dashboards": rows}


async def seed_default_dashboard(hass: HomeAssistant) -> dict[str, Any]:
    """Exercise the built-in None key before Core migrates any saved default."""
    data = hass.data[LOVELACE_DATA]
    owner = data.dashboards[None]
    assert type(owner) is LovelaceStorage and "lovelace" not in data.dashboards
    config = {"views": [{"path": "home", "title": "Native default", "cards": []}]}
    await owner.async_save(config)
    return {"config": config, "owner_type": type(owner).__name__}
