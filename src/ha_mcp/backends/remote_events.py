"""Remote event transport retained as a compatibility adapter."""

from typing import Any

import httpx

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.runtime.event_tools import event_result
from ha_mcp.tools.helpers import exception_to_structured_error


class RemoteEvents:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def fire(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        try:
            response = await self._client.fire_event(event_type, data)
        except HomeAssistantConnectionError as error:
            if isinstance(error.__cause__, httpx.TimeoutException):
                return {
                    "success": True,
                    "partial": True,
                    "event_type": event_type,
                    "message": (
                        f"Event {event_type} was dispatched but Home Assistant "
                        "did not respond within the timeout period."
                    ),
                    "warnings": [
                        "Response timed out. The event was dispatched and may still "
                        "have been delivered to subscribers."
                    ],
                }
            exception_to_structured_error(
                error,
                context={"event_type": event_type},
                suggestions=["Check Home Assistant connection"],
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"event_type": event_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify event_type is a valid identifier",
                ],
            )

        return event_result(event_type, response)
