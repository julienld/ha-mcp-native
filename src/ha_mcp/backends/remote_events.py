"""Remote event adapter using the upstream dispatch and listener contract."""

from typing import Any

from ha_mcp.tools.event_call import EventCallMixin


class RemoteEvents(EventCallMixin):
    def __init__(self, client: Any) -> None:
        self._client = client

    async def fire(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        return await self.ha_call_event(event_type, data)
