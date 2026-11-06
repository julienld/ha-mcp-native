"""Progress is optional transport output, never authority or a tool parameter."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine, progress
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.trace_tools import TraceFacade
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolFailure


@pytest.mark.parametrize("failure", ["disconnect", "blocked"])
async def test_progress_failure_cannot_mask_success_or_park_an_owned_call(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(progress, "PROGRESS_TIMEOUT", 0.01)

    async def emit(**_kwargs: Any) -> None:
        if failure == "disconnect":
            raise ConnectionError("Receiver disconnected")
        await asyncio.Future()

    report = AsyncMock(side_effect=emit)

    async def invoke(_args: dict, actor: CallContext) -> dict:
        await progress.report_progress(
            actor.progress, progress=1, total=1, message="done"
        )
        return {"saved": True}

    engine = Engine(
        SimpleNamespace(authorize=AsyncMock()),
        [ToolDefinition("example", "Example", {}, invoke)],
        operation_timeout=0.5,
    )
    result = await engine.call("example", {}, CallContext("actor", progress=report))
    assert not result.is_error and result.status == "confirmed"
    assert result.data == {"saved": True}
    report.assert_awaited_once_with(progress=1, total=1, message="done")


def test_native_trace_contract_cannot_accept_a_caller_supplied_progress_context() -> (
    None
):
    definition = define_tool(
        SimpleNamespace(),
        TraceFacade,
        "ha_get_automation_traces",
        annotations={"readOnlyHint": True},
        handler=AsyncMock(),
    )
    assert "ctx" not in definition.input_schema["properties"]
    assert definition.validate is not None
    with pytest.raises(ToolFailure, match="Extra inputs are not permitted"):
        definition.validate(
            {"automation_id": "automation.example", "ctx": {"user_id": "someone"}}
        )
