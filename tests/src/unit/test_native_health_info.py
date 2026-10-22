"""Core health presentation counts domains and owns only the probes it starts."""

import asyncio
import importlib
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import authorize
from .test_native_engine_backend import native as native


@pytest.fixture
async def health(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    rows: dict[str, Any] = {}

    async def registered(_hass: Any) -> Any:
        for domain, info in rows.items():
            yield domain, info

    owner = SimpleNamespace(
        DATA_SYSTEM_HEALTH_PLATFORMS="health_platforms",
        _registered_domain_data=registered,
        _format_value=lambda value: (
            {"type": "date", "value": value.isoformat()}
            if isinstance(value, datetime)
            else value
        ),
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.components"], "system_health", owner, raising=False
    )
    hass = SimpleNamespace(data={"health_platforms": object()})
    user = authorize(hass)
    backend = native.NativeBackend(hass)
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.health_info"
    )

    async def read(**kwargs: Any) -> dict[str, Any]:
        return await module.read_health_info(
            hass, CallContext("actor"), backend.authorize, **kwargs
        )

    return SimpleNamespace(
        read=read, rows=rows, owner=owner, user=user, hass=hass, backend=backend
    )


async def test_health_component_count_is_domains_not_initial_envelope_keys(
    health: Any,
) -> None:
    health.rows.update(
        {name: {"info": {"ok": True}} for name in ("core", "recorder", "frontend")}
    )
    result = await health.read()
    assert result["component_count"] == 3
    assert result["health_info"]["type"] == "initial"
    assert set(result["health_info"]["data"]) == set(health.rows)


async def test_health_resolves_owned_values_with_core_dates_urls_and_failures(
    health: Any,
) -> None:
    stamp = datetime(2026, 10, 1, tzinfo=UTC)

    async def date() -> datetime:
        await asyncio.sleep(0)
        return stamp

    async def broken() -> None:
        raise ValueError("provider error")

    health.rows["native_fixture"] = {
        "info": {"ready": date(), "broken": broken()},
        "manage_url": "/config/example",
        "future_metadata": True,
    }
    result = await health.read()
    domain = result["health_info"]["data"]["native_fixture"]
    assert domain["manage_url"] == "/config/example" and domain["future_metadata"]
    assert domain["info"]["ready"] == {"type": "date", "value": stamp.isoformat()}
    assert domain["info"]["broken"] == {"type": "failed", "error": "unknown"}


async def test_health_timeout_drains_owned_coroutine_without_cancelling_shared_task(
    health: Any,
) -> None:
    cleaned = asyncio.Event()

    async def owned() -> None:
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    shared = asyncio.create_task(asyncio.Event().wait())
    health.rows["native_fixture"] = {"info": {"ours": owned(), "core": shared}}
    try:
        result = await health.read(timeout=0.01)
        assert cleaned.is_set()
        assert not shared.done() and not shared.cancelling()
        info = result["health_info"]["data"]["native_fixture"]["info"]
        assert info["ours"] == {"type": "failed", "error": "timeout"}
        assert info["core"] == {"type": "pending"}
        assert result["warnings"]
    finally:
        shared.cancel()
        await asyncio.gather(shared, return_exceptions=True)


async def test_health_cancellation_does_not_cancel_shared_core_discovery(
    health: Any,
) -> None:
    discovery = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def registered(_hass: Any) -> Any:
        started.set()
        await discovery
        yield "core", {"info": {"ok": True}}

    health.owner._registered_domain_data = registered
    task = asyncio.create_task(health.read())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not discovery.cancelled()
    discovery.set_result(None)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


async def test_health_revocation_after_provider_read_blocks_disclosure(
    health: Any,
) -> None:
    async def registered(_hass: Any) -> Any:
        health.user.is_active = False
        yield "core", {"info": {"private": "value"}}

    health.owner._registered_domain_data = registered
    with pytest.raises(ToolFailure, match="active"):
        await health.read()
