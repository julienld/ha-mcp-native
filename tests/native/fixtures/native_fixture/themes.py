"""Observe Core's real configured theme library and backend selections."""

from typing import Any

from homeassistant.components.frontend import (
    DATA_DEFAULT_DARK_THEME,
    DATA_DEFAULT_THEME,
    DATA_THEMES,
)
from homeassistant.core import HomeAssistant


def theme_probe(hass: HomeAssistant) -> dict[str, Any]:
    library = hass.data[DATA_THEMES]
    return {
        "installed": sorted(library),
        "default_light": hass.data[DATA_DEFAULT_THEME],
        "default_dark": hass.data.get(DATA_DEFAULT_DARK_THEME),
        "blue_palette": library.get("native_fixture_blue"),
        "night_palette": library.get("native_fixture_night"),
    }
