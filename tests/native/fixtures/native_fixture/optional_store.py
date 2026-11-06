"""Give the isolated absence/cold-resource bench an explicit optional-store state."""

from typing import Any

_FIXTURE_ID = "native-fixture-store-absence"


async def prepare_optional_store_absence(hass: Any) -> None:
    from homeassistant.config_entries import SOURCE_IGNORE
    from homeassistant.loader import IntegrationNotFound, async_get_integration

    try:
        await async_get_integration(hass, "marketplace")
    except IntegrationNotFound:
        return
    entries = hass.config_entries.async_entries("marketplace")
    if entries:
        assert (
            len(entries) == 1
            and entries[0].source == SOURCE_IGNORE
            and entries[0].unique_id == _FIXTURE_ID
        ), entries
        return
    # Core 2026.11 queues its system discovery until HA starts. Its flow manager
    # respects an ignored entry even when that discovery was already queued.
    result = await hass.config_entries.flow.async_init(
        "marketplace",
        context={"source": SOURCE_IGNORE},
        data={"unique_id": _FIXTURE_ID, "title": "Native absence qualification"},
    )
    assert result["type"] == "create_entry", result
    entry = result["result"]
    assert entry.source == SOURCE_IGNORE and entry.unique_id == _FIXTURE_ID
    assert not hass.config_entries.async_loaded_entries("marketplace")


def store_probe(hass: Any) -> dict[str, Any]:
    return {
        "hacs_loaded": "hacs" in hass.config.components,
        "marketplace_component": "marketplace" in hass.config.components,
        "marketplace_loaded": len(
            hass.config_entries.async_loaded_entries("marketplace")
        ),
        "marketplace_entries": [
            {"source": entry.source}
            for entry in hass.config_entries.async_entries("marketplace")
        ],
    }
