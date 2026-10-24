"""Revoking a running script must stop its queued child tool effects."""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolDefinition, ToolFailure

from .test_native_code import runtime


def revoke(manager: Any, kind: str) -> None:
    if kind == "parent":
        manager.engine.enabled_tools = {"read", "write"}
    else:
        flag = "enable_beta_features" if kind == "beta" else "enable_code_mode"
        manager.policy.flags[flag] = False


@pytest.mark.parametrize(
    ("window", "revocation"),
    [
        ("script", "beta"),
        ("script", "code"),
        ("script", "parent"),
        ("authorization", "code"),
        ("approval", "parent"),
        ("approval", "beta"),
        ("after_child", "code"),
    ],
)
async def test_queued_code_child_is_refused_after_parent_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str, revocation: str
) -> None:
    from custom_components.ha_mcp_tools import native_code

    manager, _code = await runtime(tmp_path, monkeypatch)
    effect = AsyncMock(return_value={"changed": True})
    manager.engine.register(ToolDefinition("write", "Write", {}, effect))
    manager.engine.register(
        ToolDefinition(
            "read", "Read", {}, AsyncMock(return_value={}), {"readOnlyHint": True}
        )
    )
    reached, resume = asyncio.Event(), asyncio.Event()
    child_requested = False
    paused = False

    async def pause() -> None:
        nonlocal paused
        if not paused:
            paused = True
            reached.set()
            await resume.wait()

    async def authorize(_actor: CallContext) -> None:
        if child_requested and window == "authorization":
            await pause()

    async def approve(name: str, *_args: Any) -> None:
        if name == "write" and window == "approval":
            await pause()

    async def sandbox(_source: str, callbacks: dict[str, Any], *_args: Any) -> Any:
        nonlocal child_requested
        if window == "after_child":
            await callbacks["call_tool"]("read", {})
        if window in {"script", "after_child"}:
            await pause()
        child_requested = True
        return await callbacks["call_tool"]("write", {})

    manager.backend.authorize = AsyncMock(side_effect=authorize)
    manager.engine.policy_guard = approve
    monkeypatch.setattr(native_code, "run_sandboxed_code", sandbox)
    task = asyncio.create_task(
        manager.engine.call(
            "ha_manage_custom_tool",
            {
                "code": "await call_tool('write', {})",
                "justification": "Compose native tools",
            },
            CallContext("owner"),
        )
    )
    await asyncio.wait_for(reached.wait(), 2)
    revoke(manager, revocation)
    resume.set()
    result = await asyncio.wait_for(task, 2)
    effect.assert_not_awaited()
    assert "TOOL_DISABLED" in str(result.data) or "FEATURE_DISABLED" in str(result.data)


async def test_code_keeps_the_callers_child_specific_exposure_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _code = await runtime(tmp_path, monkeypatch)
    effect = AsyncMock(return_value={"changed": True})
    manager.engine.register(ToolDefinition("write", "Write", {}, effect))

    def exposure(name: str) -> None:
        if name == "write":
            raise ToolFailure(
                "TOOL_DISABLED", "Child is not exposed", status="not_started"
            )

    result = await manager.engine.call(
        "ha_manage_custom_tool",
        {"code": "await call_tool('write', {})", "justification": "Compose"},
        CallContext("owner", tool_guard=exposure),
    )
    effect.assert_not_awaited()
    assert result.data["data"]["result"]["error"]["code"] == "TOOL_DISABLED"
