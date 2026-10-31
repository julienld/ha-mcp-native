"""Imported tools cannot outlive authority while Core builds their API instance."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import LlmToolResult, install

install()

from custom_components.ha_mcp_tools import native_llm_import  # noqa: E402
from custom_components.ha_mcp_tools.const import OPT_NATIVE_IMPORT_APIS  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.tool_api import CallContext  # noqa: E402


@pytest.mark.parametrize(
    "revocation",
    [
        "read_only",
        "disabled_write",
        "disabled_read",
        "credential",
        "owner",
        "deselected",
        "bridge_disabled",
    ],
)
async def test_import_refuses_revocation_during_api_resolution(
    monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    suspended, resume = asyncio.Event(), asyncio.Event()
    state = {"credential": True}

    async def credential() -> None:
        if not state["credential"]:
            raise PermissionError("Credential was revoked")

    async def authorize(actor: CallContext) -> None:
        assert actor.credential is not None
        await actor.credential()

    backend = SimpleNamespace(authorize=authorize)
    manager = SimpleNamespace(
        hass=object(),
        backend=backend,
        engine=Engine(backend),
        entry=SimpleNamespace(options={OPT_NATIVE_IMPORT_APIS: ["fixture"]}),
    )
    owner = SimpleNamespace(id="fixture")
    registered = [owner]
    tool = SimpleNamespace(
        name="switch",
        integration="fixture",
        parameters=dict,
        annotations=SimpleNamespace(read_only=revocation == "disabled_read"),
    )
    instance = SimpleNamespace(
        api=owner,
        tools=[tool],
        async_call_tool=AsyncMock(return_value=LlmToolResult({"switched": True})),
    )
    resolutions = 0

    async def resolve(*_args: object) -> SimpleNamespace:
        nonlocal resolutions
        resolutions += 1
        if resolutions == 2:
            suspended.set()
            await resume.wait()
        return instance

    monkeypatch.setattr(
        native_llm_import,
        "llm",
        SimpleNamespace(
            LLMContext=SimpleNamespace,
            ToolInput=SimpleNamespace,
            async_get_api=resolve,
            async_get_apis=lambda _hass: registered,
        ),
    )
    monkeypatch.setattr(native_llm_import, "Context", SimpleNamespace)
    monkeypatch.setattr(
        native_llm_import, "json_bytes", lambda value: json.dumps(value).encode()
    )
    task = asyncio.create_task(
        native_llm_import.call_imported(
            manager, "fixture/switch", {}, CallContext("alice", credential=credential)
        )
    )
    await asyncio.wait_for(suspended.wait(), 1)
    if revocation == "read_only":
        manager.engine.read_only = True
    elif revocation.startswith("disabled"):
        manager.engine.enabled_tools = set()
    elif revocation == "credential":
        state["credential"] = False
    elif revocation == "owner":
        registered[:] = [SimpleNamespace(id="fixture")]
    else:
        manager.entry.options.update(
            {
                "bridge_disabled": {"enable_native_llm_import": False},
                "deselected": {OPT_NATIVE_IMPORT_APIS: []},
            }[revocation]
        )
    resume.set()
    result = await asyncio.wait_for(task, 1)
    instance.async_call_tool.assert_not_awaited()
    assert result.is_error and result.status == "not_started"
