"""Describing custom cards shares the explicit resource initialization effect."""

import importlib
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_resource_service import resources as resources


@pytest.fixture
def describe_runtime(resources, monkeypatch):
    from custom_components.ha_mcp_tools.native_dashboard_tools import (
        build_dashboard_tools,
    )

    manager, _engine, owner, *_ = resources
    module = importlib.import_module("custom_components.ha_mcp_tools.card_descriptions")
    producer = AsyncMock(
        return_value={
            "success": True,
            "fields": [{"name": "future", "selector": {"text": {}}}],
            "field_coverage": "native",
        }
    )
    monkeypatch.setattr(module, "async_describe_card", producer)
    return Engine(manager.backend, build_dashboard_tools(manager)), owner, producer


async def test_builtin_card_describe_is_readonly_without_resource_initialization(
    describe_runtime,
):
    engine, owner, producer = describe_runtime
    owner.loaded = False
    result = await engine.call(
        "ha_config_get_dashboard",
        {"describe": True, "card_type": "tile"},
        CallContext("owner", read_only=True),
    )
    assert not result.is_error and owner.loads == 0, result.data
    assert result.data["fields"][0]["name"] == "future"
    assert result.data["field_coverage"] == "native"
    assert producer.await_args.kwargs["resource_items"] == []


async def test_custom_describe_requires_admission_only_until_core_resources_are_loaded(
    describe_runtime,
):
    engine, owner, producer = describe_runtime
    owner.loaded = False
    with pytest.raises(ToolFailure):
        await engine.call(
            "ha_config_get_dashboard",
            {"describe": True},
            CallContext("owner", read_only=True),
        )
    producer.assert_not_awaited()
    listed = await engine.call(
        "ha_config_get_dashboard", {"describe": True}, CallContext("owner")
    )
    assert not listed.is_error and owner.loads == 1
    again = await engine.call(
        "ha_config_get_dashboard",
        {"describe": True, "card_type": "custom:demo"},
        CallContext("owner", read_only=True),
    )
    assert not again.is_error and owner.loads == 1
    assert producer.await_args.kwargs["resource_items"] == list(owner.data.values())


async def test_resource_reload_after_read_admission_cannot_migrate_during_describe(
    describe_runtime,
):
    engine, owner, producer = describe_runtime

    async def reload_resources(name, arguments, actor, writes):
        assert writes is False
        owner.loaded = False

    engine.policy_guard = reload_resources
    result = await engine.call(
        "ha_config_get_dashboard",
        {"describe": True, "card_type": "custom:demo"},
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started" and owner.loads == 0
    assert result.data["error"]["code"] == "READ_ONLY"
    producer.assert_not_awaited()
