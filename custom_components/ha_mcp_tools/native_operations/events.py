"""Emit events through Core's real bus using the authenticated actor's context."""

import copy
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.runtime.event_tools import event_result, validate_event_type
from ha_mcp.tool_api import CallContext, ToolFailure

from .registry_access import Authorize, admit_registry_write, registry_failure


@dataclass(slots=True)
class NativeEvents:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)

    async def fire(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        from homeassistant.const import EVENT_STATE_CHANGED
        from homeassistant.core import Context, EventOrigin, State

        try:
            validate_event_type(event_type)
            payload = copy.deepcopy(data)
            # Preserve APIEventView's conversion through the actual Core type;
            # callers cannot supply saved Contexts as the new event's authority.
            if event_type == EVENT_STATE_CHANGED and payload:
                for key in ("old_state", "new_state"):
                    if state := State.from_dict(payload[key]):
                        payload[key] = state
            await admit_registry_write(self.actor, self.authorize)
        except Exception as error:
            raise registry_failure(error, {"event_type": event_type}) from error

        context = Context(user_id=self.actor.user_id)
        try:
            self.hass.bus.async_fire(event_type, payload, EventOrigin.remote, context)
        except Exception as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                f"Core raised {type(error).__name__} during event emission. The event may have been delivered; inspect its subscribers before retrying.",
                status="outcome_unknown",
            ) from error
        return event_result(event_type, {})
