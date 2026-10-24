"""Internal budgets are bounded and shared by direct and transport invocations."""

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult

from .test_native_engine import Backend


@pytest.mark.parametrize("owned", [False, True])
async def test_registered_budget_is_shared_by_tool_and_owned_entry_points(
    owned: bool,
) -> None:
    async def finish(*_args: Any) -> dict[str, bool]:
        await asyncio.sleep(0.03)
        return {"done": True}

    def selector(args: dict[str, Any]) -> float | None:
        return 1.0 if args["long"] else None

    definition = ToolDefinition("work", "Work", {}, finish, operation_timeout=selector)
    engine = Engine(Backend(), [definition], operation_timeout=0.01)
    args = {"long": True}
    result = (
        await engine.call_owned(
            "work", finish, CallContext("actor"), writes=True, arguments=args
        )
        if owned
        else await engine.call("work", args, CallContext("actor"))
    )
    assert result.status == "confirmed" and result.data == {"done": True}


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True, "30"])
async def test_invalid_registered_budget_never_dispatches(budget: Any) -> None:
    invoke = AsyncMock()
    definition = ToolDefinition(
        "work", "Work", {}, invoke, operation_timeout=lambda _args: budget
    )
    engine = Engine(Backend(), [definition])
    with pytest.raises(ValueError, match="finite positive"):
        await engine.call("work", {}, CallContext("actor"))
    invoke.assert_not_awaited()
    assert not engine.operations


@pytest.mark.parametrize("declared", [False, True])
async def test_expiration_keeps_its_outcome_and_runs_the_safety_filter(
    declared: bool,
) -> None:
    async def blocked(*_args: Any) -> None:
        await asyncio.Event().wait()

    async def project(
        _name: str, _args: dict, _actor: CallContext, result: ToolResult
    ) -> ToolResult:
        await asyncio.sleep(0)
        return replace(result, data={**result.data, "filtered": True})

    definition = ToolDefinition(
        "work",
        "Work",
        {},
        AsyncMock(side_effect=blocked),
        operation_timeout=(lambda _args: 0.01) if declared else None,
    )
    engine = Engine(
        Backend(),
        [definition],
        operation_timeout=1 if declared else 0.01,
        result_filter=project,
    )
    result = await engine.call("work", {}, CallContext("actor"))
    assert result.status == "outcome_unknown" and result.is_error
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    assert result.data["filtered"]
    definition.handler.assert_awaited_once()


async def test_result_filter_has_its_own_finite_budget() -> None:
    async def blocked(*_args: Any) -> ToolResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    invoke = AsyncMock(return_value={"private": "withheld"})
    definition = ToolDefinition("work", "Work", {}, invoke)
    engine = Engine(
        Backend(), [definition], operation_timeout=0.01, result_filter=blocked
    )
    result = await asyncio.wait_for(engine.call("work", {}, CallContext("actor")), 1)
    assert result.status == "confirmed" and result.is_error
    assert result.data["error"]["code"] == "RESULT_FILTER_FAILED"
    assert "private" not in str(result.data)
