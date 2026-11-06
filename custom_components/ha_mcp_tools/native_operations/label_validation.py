"""Normalize label assignments using the current registry before an owner write."""

from typing import cast

from homeassistant.core import HomeAssistant


def existing_label_ids(hass: HomeAssistant, label_ids: set[str]) -> set[str]:
    from homeassistant.helpers import label_registry as lr

    if missing := getattr(lr, "async_get_missing_label_ids", None):
        return label_ids - cast(set[str], missing(hass, label_ids))
    # Core 2026.8 has the public lookup, but not September's bulk helper.
    registry = lr.async_get(hass)
    return {
        label_id
        for label_id in label_ids
        if registry.async_get_label(label_id) is not None
    }
