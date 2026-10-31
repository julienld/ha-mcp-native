"""Observe real Core event delivery without constructing a transport handler."""

from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

EVENT_TYPE = "native_fixture_custom_event"


def observe_events(hass: HomeAssistant) -> None:
    records: list[dict[str, Any]] = []
    hass.data["native_fixture"]["events"] = records

    @callback
    def record(event: Event) -> None:
        records.append(
            {
                "event_type": event.event_type,
                "data": event.data,
                "user_id": event.context.user_id,
                "context_id": event.context.id,
                "origin": str(event.origin),
            }
        )
        del records[:-2]

    hass.bus.async_listen(EVENT_TYPE, record)
