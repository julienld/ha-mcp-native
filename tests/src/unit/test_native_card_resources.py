"""Deferred card inspection consumes only the resource values already admitted."""

import asyncio
import copy
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_native_engine_backend import native as native
from .test_native_resource_service import resources as resources


@pytest.mark.parametrize(
    "items", [[], [{"id": "one", "url": "/local/card.js", "type": "module"}]]
)
async def test_custom_refresh_after_timeout_never_reloads_core_resources(
    native, monkeypatch, tmp_path, items
):
    module = importlib.import_module("custom_components.ha_mcp_tools.custom_cards")
    release, started = asyncio.Event(), asyncio.Event()
    captured = []
    read_core = AsyncMock(
        side_effect=AssertionError("Core resources were already captured")
    )
    monkeypatch.setattr(module, "_async_resource_files", read_core)
    monkeypatch.setattr(module, "_refresh_task", None)
    monkeypatch.setattr(module, "_REFRESH_LOCK", asyncio.Lock())
    monkeypatch.setattr(module, "_unsupported_resources", [])
    custom = SimpleNamespace(_dom="loaded", refresh=lambda *args: None)
    monkeypatch.setattr(module, "_custom", custom)

    def files(directory, rows):
        captured.append(copy.deepcopy(rows))
        return {}

    monkeypatch.setattr(module, "_resource_files", files)

    async def executor(function, *args):
        if function is files:
            started.set()
            await release.wait()
        return function(*args)

    hass = SimpleNamespace(
        config=SimpleNamespace(config_dir=str(tmp_path)),
        async_create_background_task=lambda coro, name: asyncio.create_task(coro),
        async_add_executor_job=executor,
    )
    original = copy.deepcopy(items)
    try:
        result = await module.async_get_custom_cards(
            hass, timeout=0.01, resource_items=items
        )
        assert result is custom and started.is_set() and not module._refresh_task.done()
        items.append({"id": "late", "url": "/local/late.js", "type": "module"})
    finally:
        release.set()
        await module._refresh_task
    assert captured == [original]
    read_core.assert_not_awaited()


async def test_postwrite_advice_never_initializes_resources_and_invalidates_the_saved_hash(
    resources,
):
    from custom_components.ha_mcp_tools.native_operations.dashboard_advice import (
        DashboardAdvice,
    )
    from ha_mcp.tool_api import CallContext

    manager, _engine, owner, *_ = resources
    owner.loaded = False
    warnings = await DashboardAdvice(manager, CallContext("owner")).warnings(
        {"views": [{"cards": [{"type": "custom:demo"}]}]}
    )
    assert owner.loads == 0 and owner.loaded is False
    assert any(
        "resource" in warning.lower() and "not" in warning.lower()
        for warning in warnings
    )
