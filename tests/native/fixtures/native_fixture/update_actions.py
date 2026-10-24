"""Actual Core UpdateEntity providers, including one uncertain after-effect result."""

from typing import Any

from homeassistant.components.update import (
    DATA_COMPONENT,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

DOMAIN = "native_fixture"
ISSUE = "native_update_check"


class FixtureUpdate(UpdateEntity):
    _attr_should_poll = False
    _attr_installed_version = "1.0"
    _attr_latest_version = "2.0"
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.BACKUP
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, name: str, *, uncertain: bool = False) -> None:
        self._attr_name = f"Native fixture update {name}"
        self._attr_unique_id = f"native-fixture-update-{name}"
        self._attr_extra_state_attributes = {"native_future_field": {"schema": "next"}}
        self.uncertain = uncertain
        self.installs: list[dict[str, Any]] = []

    async def async_release_notes(self) -> str:
        return "Actual native fixture provider release notes for 2.0"

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        self.installs.append(
            {"backup": backup, "version": version, "actor": self._context.user_id}
        )
        self._attr_installed_version = version or self.latest_version
        self.async_write_ha_state()
        if self.uncertain:
            raise HomeAssistantError(
                "Fixture update completed its effect before failing"
            )


async def seed_updates(hass: HomeAssistant) -> dict[str, Any]:
    if "updates" not in hass.data[DOMAIN]:
        assert await async_setup_component(hass, "update", {})
        entities = {
            "normal": FixtureUpdate("normal"),
            "uncertain": FixtureUpdate("uncertain", uncertain=True),
            "tail": FixtureUpdate("tail"),
        }
        await hass.data[DATA_COMPONENT].async_add_entities(list(entities.values()))
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE,
        )
        hass.data[DOMAIN]["updates"] = entities
    return update_probe(hass)


def update_probe(hass: HomeAssistant) -> dict[str, Any]:
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
    return {
        "entities": {
            key: entity.entity_id
            for key, entity in hass.data[DOMAIN]["updates"].items()
        },
        "installs": {
            key: list(entity.installs)
            for key, entity in hass.data[DOMAIN]["updates"].items()
        },
        "issue": {
            "domain": DOMAIN,
            "issue_id": ISSUE,
            "dismissed_version": issue.dismissed_version if issue is not None else None,
        },
    }
