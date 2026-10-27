"""Public event contract shared by Core and the remote compatibility adapter."""

from typing import Annotated, Any, Protocol

from pydantic import Field

from ha_mcp.errors import create_validation_error
from ha_mcp.tool_api import ToolFailure
from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .action_params import _parse_json_dict_param
from .errors import raise_failure


class Events(Protocol):
    async def fire(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any]: ...


def validate_event_type(event_type: str) -> None:
    if not event_type or not event_type.strip():
        raise_failure(
            create_validation_error(
                "event_type cannot be empty or whitespace", parameter="event_type"
            )
        )
    if "/" in event_type or "\\" in event_type:
        raise_failure(
            create_validation_error(
                "event_type cannot contain path separators",
                parameter="event_type",
                details=f"Received: {event_type!r}",
            )
        )


def event_result(event_type: str, response: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "event_type": event_type,
        "message": response.get("message", f"Event {event_type} fired."),
    }


class EventTools:
    def __init__(self, events: Events) -> None:
        self.events = events

    async def ha_call_event(
        self,
        event_type: Annotated[
            str,
            Field(description="Event type to fire, e.g. 'my_custom_event'."),
        ],
        data: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional event payload, delivered to subscribers as the "
                    "event's data (trigger.event.data in an automation)."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Execute a custom event on the Home Assistant event bus.

        When NOT to use: for controlling entities (lights, switches, climate) — use
        ha_call_service instead. To run an automation now, use
        ha_config_set_automation(identifier=..., run_actions=True).

        Use this to publish custom event types consumed by event-triggered automations,
        Node-RED flows, or custom integrations that subscribe to specific event types.

        Caveats: Events are fire-and-forget; this tool confirms the event was accepted
        by the bus but does not verify whether any automation or subscriber acted on it.
        """
        try:
            validate_event_type(event_type)
            parsed = _parse_json_dict_param(
                data, type_error_message="Event data must be a JSON object (dict)"
            )
        except ToolFailure as error:
            raise ToolFailure(
                error.code, str(error), data=error.data, status="not_started"
            ) from error
        return await self.events.fire(event_type, parsed)
