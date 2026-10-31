"""Long Core jobs retain their declared budget through Engine and projection."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime.engine import Engine
from ha_mcp.tool_api import CallContext, ToolResult

from .test_native_app_actions import actions as actions
from .test_native_apps import apps as apps
from .test_native_engine_backend import native as native
from .test_native_full_backup_restore import restore as restore
from .test_native_full_backups import backups as backups


async def _project(
    _name: str, _arguments: dict[str, Any], _actor: CallContext, result: ToolResult
) -> ToolResult:
    await asyncio.sleep(0)
    return result


@pytest.mark.parametrize("action", ["install", "check_updates"])
async def test_app_job_outlives_default_engine_deadline(
    actions: Any, action: str
) -> None:
    from custom_components.ha_mcp_tools.native_app_tools import app_definitions

    method = (
        actions.client.store.install_addon
        if action == "install"
        else actions.client.store.reload
    )

    async def complete(*_args: Any) -> None:
        await asyncio.sleep(0.03)

    method.side_effect = complete
    engine = Engine(
        actions.manager.backend,
        tools=app_definitions(actions.manager),
        operation_timeout=0.01,
        result_filter=_project,
    )
    result = await engine.call(
        "ha_manage_app",
        {
            "action": action,
            **({"slug": "core_fixture"} if action == "install" else {}),
        },
        CallContext("actor"),
    )
    method.assert_awaited_once()
    assert result.status == "confirmed" and not result.is_error
    assert result.data["success"]


@pytest.mark.parametrize("action", ["create", "restore"])
async def test_full_backup_job_outlives_default_engine_deadline(
    restore: Any, action: str
) -> None:
    from custom_components.ha_mcp_tools.native_backup_tools import backup_definition

    original = restore.owner.async_create_backup.side_effect

    async def complete(**arguments: Any) -> Any:
        await asyncio.sleep(0.03)
        return await original(**arguments)

    restore.owner.async_create_backup.side_effect = complete
    engine = Engine(
        restore.manager.backend,
        tools=[backup_definition(restore.manager)],
        operation_timeout=0.01,
        result_filter=_project,
    )
    try:
        result = await engine.call(
            "ha_manage_backup",
            {"scope": "snapshot", "action": action, "backup_id": "old"},
            CallContext("actor"),
        )
        restore.owner.async_create_backup.assert_awaited_once()
        assert "created" in restore.rows
        assert not result.is_error
        assert result.status == ("confirmed" if action == "create" else "scheduled")
        if action == "restore":
            assert result.data["safety_backup_id"] == "created"
            assert result.data["restore_completed"] is False
    finally:
        await asyncio.gather(*tuple(restore.manager.system_lifecycle.pending))
        await asyncio.sleep(0)


async def test_app_websocket_keeps_its_collection_and_close_budget(
    actions: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_app_tools import app_definitions
    from custom_components.ha_mcp_tools.native_operations import apps as app_module

    route = SimpleNamespace(
        slug="core_fixture", addon={"name": "Fixture"}, debug_url="fixture"
    )
    monkeypatch.setattr(app_module.AppRoute, "prepare", AsyncMock(return_value=route))

    async def collect(*_args: Any) -> tuple[Any, ...]:
        await asyncio.sleep(0.03)
        return [], 0, "timeout", 0.03, 1000

    exchange = AsyncMock(side_effect=collect)
    monkeypatch.setattr(app_module, "ws_exchange", exchange)
    actions.manager.hass.async_add_executor_job = asyncio.to_thread
    engine = Engine(
        actions.manager.backend,
        tools=app_definitions(actions.manager),
        operation_timeout=0.01,
        result_filter=_project,
    )
    result = await engine.call(
        "ha_manage_app",
        {
            "slug": "core_fixture",
            "path": "/events",
            "websocket": True,
            "wait_for_close": True,
        },
        CallContext("actor"),
    )
    exchange.assert_awaited_once()
    assert not result.is_error and result.status == "confirmed"


async def test_code_uses_its_loaded_operator_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools import native_code

    from .test_native_code import runtime

    monkeypatch.setenv("CODE_MODE_MAX_DURATION", "300")
    manager, code = await runtime(tmp_path, monkeypatch)

    async def sandbox(_source: str, _callbacks: Any, settings: Any, *_args: Any) -> int:
        assert settings is code.settings and settings.code_mode_max_duration > 120
        await asyncio.sleep(0.03)
        return 42

    monkeypatch.setattr(native_code, "run_sandboxed_code", sandbox)
    manager.engine.operation_timeout = 0.01
    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {"code": "40 + 2", "justification": "Check the configured duration"},
        CallContext("actor"),
    )
    assert not result.is_error and result.status == "confirmed"
    assert result.data["data"]["result"] == 42
