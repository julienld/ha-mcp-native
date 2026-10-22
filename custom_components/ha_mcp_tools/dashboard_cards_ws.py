"""The ``dashboard_cards`` websocket command: the card type list, or one card
type's form, from ``card_definitions``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import card_definitions as cd
from .card_descriptions import async_describe_card

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """``dashboard_cards``: the card type list, or one card type's form."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await async_describe_card(hass, msg.get("card_type"))}

    def do(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): cd.WS_DASHBOARD_CARDS,
                vol.Optional("card_type"): str,
            },
            do,
            prep,
        )
    ]
