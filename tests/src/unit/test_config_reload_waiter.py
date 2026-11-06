"""Configuration reload acknowledgements are observed independently of writes."""

from typing import Any

import pytest

from ha_mcp.tools import config_write_helpers


class _FakeWsClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.handlers: dict[str, list[Any]] = {}
        self.unsubscribed: list[int] = []

    def add_event_handler(self, event_type: str, handler: Any) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    def remove_event_handler(self, event_type: str, handler: Any) -> None:
        self.handlers[event_type].remove(handler)

    async def subscribe_events(self, event_type: str) -> int:
        return 7

    async def unsubscribe_events(self, subscription_id: int) -> None:
        self.unsubscribed.append(subscription_id)

    async def fire(self, event_type: str) -> None:
        for handler in list(self.handlers.get(event_type, [])):
            await handler({"event_type": event_type, "data": {}})


@pytest.mark.unit
@pytest.mark.anyio
async def test_reload_waiter_returns_true_after_reload_event(monkeypatch) -> None:
    ws = _FakeWsClient()

    async def get_ws(_client):
        return ws

    monkeypatch.setattr(config_write_helpers, "_get_waiter_ws_client", get_ws)

    async with config_write_helpers.config_reload_waiter(
        object(), "automation_reloaded"
    ) as wait_for_reload:
        await ws.fire("automation_reloaded")
        assert await wait_for_reload() is True

    assert ws.unsubscribed == [7]
    assert ws.handlers["automation_reloaded"] == []


@pytest.mark.unit
@pytest.mark.anyio
async def test_reload_waiter_times_out_and_skips_without_ws(monkeypatch) -> None:
    ws = _FakeWsClient()

    async def get_ws(_client):
        return ws

    monkeypatch.setattr(config_write_helpers, "_get_waiter_ws_client", get_ws)
    async with config_write_helpers.config_reload_waiter(
        object(), "automation_reloaded", timeout=0.01
    ) as wait_for_reload:
        assert await wait_for_reload() is False

    async with config_write_helpers.config_reload_waiter(
        object(), "automation_reloaded", enabled=False
    ) as wait_for_reload:
        assert await wait_for_reload() is None
