"""LLM metadata and result contracts with the Core 2026.10 fixture baseline."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ._embedded_stubs import LlmToolAnnotations, LlmToolResult, install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DOMAIN,
    EXPOSURE_TOOL_SEARCH,
)

from ._llm_api_helpers import (  # noqa: E402
    fake_session,
    make_api,
    make_hass,
    tool_entry,
)


def _annotated_entry(sdk: str, **hints: bool) -> SimpleNamespace:
    """A stamped tool entry whose annotations come from a real SDK model."""
    if sdk == "v1":
        from mcp import types as v1_types

        annotations = v1_types.ToolAnnotations(title="Get Entity State", **hints)
    else:
        from ha_mcp._vendor.mcp import types as v2_types

        annotations = v2_types.ToolAnnotations.model_validate(
            {"title": "Get Entity State", **hints}
        )
    entry = tool_entry("ha_get_state")
    entry.annotations = annotations
    return entry


class TestCoreToolMetadata:
    """Home Assistant 2026.10 reads tool titles, safety hints and ToolResult."""

    @pytest.mark.parametrize("sdk", ["v1", "v2"])
    async def test_mirrored_tool_carries_the_servers_metadata(
        self, monkeypatch: pytest.MonkeyPatch, sdk: str
    ) -> None:
        hints = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        fake_session(monkeypatch, tools=[_annotated_entry(sdk, **hints)])

        instance = await make_api(make_hass()).async_get_api_instance(
            llm_api.llm.LLMContext()
        )

        (tool,) = instance.tools
        assert tool.title == "Get Entity State"
        assert tool.integration == DOMAIN
        assert tool.annotations == LlmToolAnnotations(
            read_only=True, destructive=False, idempotent=True, open_world=False
        )

    async def test_an_undeclared_hint_keeps_cores_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_session(monkeypatch, tools=[_annotated_entry("v2", readOnlyHint=True)])

        instance = await make_api(make_hass()).async_get_api_instance(
            llm_api.llm.LLMContext()
        )

        assert instance.tools[0].annotations == LlmToolAnnotations(read_only=True)

    @pytest.mark.parametrize("is_error", [True, False])
    async def test_call_returns_a_tool_result_flagging_errors(
        self, monkeypatch: pytest.MonkeyPatch, is_error: bool
    ) -> None:
        from ha_mcp._vendor.mcp.types import CallToolResult, TextContent

        fake_session(
            monkeypatch,
            call_result=CallToolResult(
                content=[TextContent(type="text", text="x")], is_error=is_error
            ),
        )
        tool = llm_api.HaMcpTool(
            "ha_search",
            "Search",
            llm_api.vol.Schema({}),
            "http://127.0.0.1:9584/private_x",
        )

        result = await tool.async_call(
            make_hass(),
            llm_api.llm.ToolInput("ha_search", {}),
            llm_api.llm.LLMContext(),
        )

        assert isinstance(result, LlmToolResult)
        assert result.error is is_error
        assert result.data["isError"] is is_error

    async def test_meta_tools_declare_their_own_safety(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_session(monkeypatch, tools=[tool_entry("ha_get_state")])
        instance = await make_api(
            make_hass(), mode=EXPOSURE_TOOL_SEARCH
        ).async_get_api_instance(llm_api.llm.LLMContext())
        search, call = instance.tools

        assert search.annotations == LlmToolAnnotations(
            read_only=True, destructive=False, idempotent=True, open_world=False
        )
        # The dispatcher can run any exposed tool: Core's least-safe default.
        assert call.annotations == LlmToolAnnotations()
        assert {search.integration, call.integration} == {DOMAIN}

        unknown = await call.async_call(
            make_hass(),
            llm_api.llm.ToolInput("ha_call_tool", {"name": "ha_nope"}),
            llm_api.llm.LLMContext(),
        )
        assert unknown.error is True
