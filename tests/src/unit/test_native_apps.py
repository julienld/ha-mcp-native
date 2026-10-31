"""App reads borrow Core's Supervisor owner and retain native metadata safely."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.redaction import REDACTED_SET
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation

from .test_native_engine_backend import native as native


@pytest.fixture
def apps(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    row = {
        "slug": "core_fixture",
        "name": "Fixture",
        "state": "started",
        "version": "1.0",
        "version_latest": "2.0",
        "future_field": {"enabled": True},
        "options": {"password": "fixture-secret"},
        "schema": [{"name": "password", "type": "string", "format": "password"}],
    }
    replies = {
        "/addons": {"addons": [row]},
        "/addons/core_fixture/info": row,
        "/addons/core_fixture/stats": {"cpu_percent": 1, "future_stat": 2},
        "/store": {
            "addons": [row],
            "repositories": [{"slug": "core", "future_repository": True}],
        },
    }

    async def read(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"method": "get", "timeout": 30, "source": "ha_mcp_tools"}
        return {"result": "ok", "data": copy.deepcopy(replies[endpoint])}

    raw = SimpleNamespace(
        websession=SimpleNamespace(closed=False),
        send_command=AsyncMock(side_effect=read),
    )
    client = SimpleNamespace()
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.hassio",
        SimpleNamespace(get_supervisor_client=lambda hass: client),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.hassio.const",
        SimpleNamespace(DATA_COMPONENT="hassio"),
    )
    manager = SimpleNamespace(
        hass=SimpleNamespace(
            config=SimpleNamespace(components={"hassio"}), data={"hassio": raw}
        ),
        backend=SimpleNamespace(authorize=AsyncMock()),
        policy=SimpleNamespace(flag=lambda key: True, flags={}),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.app_reads"
    )
    service = module.NativeAppReads(manager, CallContext("actor", read_only=True))
    return SimpleNamespace(
        manager=manager, service=service, raw=raw, replies=replies, client=client
    )


async def read(apps: Any, **kwargs: Any) -> dict[str, Any]:
    values = {
        "source": None,
        "slug": None,
        "include_stats": False,
        "repository": None,
        "query": None,
        **kwargs,
    }
    return await apps.service.get_addon(**values)


async def test_native_app_details_keep_future_fields_and_schema_redaction(
    apps: Any,
) -> None:
    result = await read(apps, slug="core_fixture")
    assert result["addon"]["future_field"] == {"enabled": True}
    assert result["addon"]["options"]["password"] == REDACTED_SET
    apps.raw.send_command.assert_awaited_once()


async def test_native_app_inventories_keep_provider_and_stats_fields(apps: Any) -> None:
    installed = await read(apps, include_stats=True)
    assert installed["summary"]["running"] == 1
    assert installed["addons"][0]["stats"]["future_stat"] == 2
    assert installed["addons"][0]["future_field"] == {"enabled": True}
    assert installed["addons"][0]["options"]["password"] == REDACTED_SET
    available = await read(apps, source="available", query="fixture")
    assert available["addons"][0]["version"] == "2.0"
    assert available["addons"][0]["version_installed"] == "1.0"
    assert available["repositories"][0]["future_repository"]
    assert available["addons"][0]["options"]["password"] == REDACTED_SET


async def test_native_app_uses_its_operator_list_for_unmarked_secrets(
    apps: Any,
) -> None:
    apps.manager.policy.flags["redact_app_option_keys"] = "core_fixture:backup_password"
    row = apps.replies["/addons/core_fixture/info"]
    row["options"]["backup_password"] = "native-scoped-backup-secret"
    row["schema"].append({"name": "backup_password", "type": "string"})

    result = await read(apps, slug="core_fixture")

    assert result["addon"]["options"]["backup_password"] == REDACTED_SET


async def test_missing_supervisor_is_explicit_without_connection_discovery(
    apps: Any,
) -> None:
    apps.manager.hass.config.components.clear()
    with pytest.raises(UnsupportedOperation):
        await read(apps)
    apps.raw.send_command.assert_not_awaited()


async def test_core_owner_replacement_during_read_withholds_result(apps: Any) -> None:
    async def replace(*args: Any, **kwargs: Any) -> dict[str, Any]:
        apps.manager.hass.data["hassio"] = SimpleNamespace(
            websession=SimpleNamespace(closed=False)
        )
        return {"result": "ok", "data": {"private": "must not return"}}

    apps.raw.send_command.side_effect = replace
    with pytest.raises(ToolFailure) as error:
        await read(apps)
    assert error.value.code == "CONFIG_CONFLICT"
    assert "must not return" not in str(error.value.data)


async def test_parallel_stats_drain_before_auth_failure_leaves_tool(apps: Any) -> None:
    second_entered, release = asyncio.Event(), asyncio.Event()
    apps.replies["/addons"]["addons"].append(
        {"slug": "core_second", "state": "started"}
    )
    original = apps.raw.send_command.side_effect

    async def stats(endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if endpoint == "/addons/core_second/stats":
            second_entered.set()
            await release.wait()
            return {"result": "ok", "data": {"cpu_percent": 0}}
        if endpoint == "/addons/core_fixture/stats":
            await second_entered.wait()
            apps.manager.backend.authorize.side_effect = PermissionError("revoked")
        return await original(endpoint, **kwargs)

    apps.raw.send_command.side_effect = stats
    task = asyncio.create_task(read(apps, include_stats=True))
    try:
        await asyncio.wait_for(second_entered.wait(), 1)
        for _ in range(8):
            await asyncio.sleep(0)
        assert not task.done(), "A parallel Supervisor request outlived its tool owner"
    finally:
        release.set()
        with pytest.raises(ToolFailure):
            _ = await task
