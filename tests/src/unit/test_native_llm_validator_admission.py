"""Final authorization cannot leave an imported tool using a stale validator."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ._embedded_stubs import LlmToolResult, install

install()

from custom_components.ha_mcp_tools import native_llm_import  # noqa: E402
from custom_components.ha_mcp_tools.const import OPT_NATIVE_IMPORT_APIS  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext  # noqa: E402


async def test_imported_arguments_use_the_validator_after_final_authorization(
    monkeypatch,
):
    state = {"resolved": 0}

    def reject(_arguments):
        raise ValueError("The authoritative schema no longer accepts these arguments")

    tool = SimpleNamespace(
        name="change",
        integration="fixture",
        parameters=dict,
        annotations=SimpleNamespace(read_only=False),
    )
    owner = SimpleNamespace(id="fixture")
    instance = SimpleNamespace(
        api=owner,
        tools=[tool],
        async_call_tool=AsyncMock(return_value=LlmToolResult({"changed": True})),
    )

    async def authorize(_actor):
        if state["resolved"] == 2:
            tool.parameters = reject

    async def resolve(*_args):
        state["resolved"] += 1
        return instance

    backend = SimpleNamespace(authorize=authorize)
    manager = SimpleNamespace(
        hass=object(),
        backend=backend,
        engine=Engine(backend),
        entry=SimpleNamespace(options={OPT_NATIVE_IMPORT_APIS: ["fixture"]}),
    )
    monkeypatch.setattr(
        native_llm_import,
        "llm",
        SimpleNamespace(
            LLMContext=SimpleNamespace,
            ToolInput=SimpleNamespace,
            async_get_api=resolve,
            async_get_apis=lambda _hass: [owner],
        ),
    )
    monkeypatch.setattr(native_llm_import, "Context", SimpleNamespace)
    monkeypatch.setattr(
        native_llm_import, "json_bytes", lambda value: json.dumps(value).encode()
    )
    result = await native_llm_import.call_imported(
        manager, "fixture/change", {}, CallContext("actor")
    )
    instance.async_call_tool.assert_not_awaited()
    assert result.is_error and result.status == "not_started", result
