"""Read Frontend's actual installed theme library and selected defaults."""

from typing import Any

from .registry_access import registry_json


def theme_image(hass: Any) -> dict[str, Any]:
    from homeassistant.components.frontend import (
        DATA_DEFAULT_DARK_THEME,
        DATA_DEFAULT_THEME,
        DATA_THEMES,
    )

    if hass.config.recovery_mode or hass.config.safe_mode:
        return {"themes": {}, "default_theme": "default"}
    return registry_json(
        {
            "themes": hass.data[DATA_THEMES],
            "default_theme": hass.data[DATA_DEFAULT_THEME],
            "default_dark_theme": hass.data.get(DATA_DEFAULT_DARK_THEME),
        }
    )
