"""Assist projection keeps its public contract without an MCP dependency."""

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import ToolDefinition, ToolResult
from ha_mcp.tool_api.llm_projection import describe_tool, result_envelope

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import llm_projection, native_llm  # noqa: E402
from custom_components.ha_mcp_tools.native_server_policy import NativePolicy  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402


@pytest.mark.parametrize("target", ["neutral", "native_adapter"])
def test_projection_imports_without_sdk_or_legacy_runtime(target: str) -> None:
    script = """
        import sys
        from importlib.abc import MetaPathFinder
        if TARGET == 'native_adapter':
            from tests.src.unit._embedded_stubs import install
            install()
            from types import ModuleType
            from pathlib import Path
            package = ModuleType('custom_components.ha_mcp_tools')
            package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
            sys.modules['custom_components.ha_mcp_tools'] = package
        forbidden = (
            'fastmcp', 'mcp', 'ha_mcp._vendor', 'ha_mcp.client',
            'ha_mcp.server', 'ha_mcp.config',
            'custom_components.ha_mcp_tools.native_server',
            'custom_components.ha_mcp_tools.llm_api',
        )
        if TARGET == 'neutral':
            forbidden += ('homeassistant',)
        class RejectTransport(MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + '.') for name in forbidden):
                    raise AssertionError('Projection imported ' + fullname)
        sys.meta_path.insert(0, RejectTransport())
        if TARGET == 'neutral':
            from ha_mcp.tool_api.llm_projection import describe_tool, result_envelope
        else:
            from custom_components.ha_mcp_tools import native_llm
    """
    result = subprocess.run(
        [sys.executable, "-c", f"TARGET = {target!r}\n" + textwrap.dedent(script)],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "payload",
    [{"value": [None, 3], "meta": {"_meta": "kept"}}, [1, "été"], "caption", None],
)
def test_neutral_envelope_keeps_existing_assist_result(payload: object) -> None:
    from custom_components.ha_mcp_tools.llm_api import _meta_as_field_name
    from custom_components.ha_mcp_tools.native_server import mcp_result

    result = ToolResult(payload, is_error=True, status="pending", operation_id="owned")
    previous = _meta_as_field_name(
        mcp_result(result, allow_continuation=False)
        .to_mcp_result()
        .model_dump(exclude_unset=True, exclude_none=True, by_alias=True)
    )
    assert result_envelope(result) == previous


async def test_native_search_keeps_unconvertible_pin_discoverable_without_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = SimpleNamespace(authorize=AsyncMock())
    definitions = [
        ToolDefinition(
            name,
            f"Describe {name}",
            {"type": "object", "properties": {}, "x-convertible": convertible},
            AsyncMock(),
            annotations={"title": name.title(), "readOnlyHint": True},
            meta={"ha_mcp": {"native_support": {"note": "Native owner"}}},
        )
        for name, convertible in (("converted", True), ("unconverted", False))
    ]
    manager = SimpleNamespace(
        backend=backend,
        engine=Engine(backend, definitions),
        refresh_policy=AsyncMock(),
        entry=SimpleNamespace(options={"enable_llm_api": True}),
        policy=NativePolicy(pinned=frozenset({"converted", "unconverted"})),
        instructions="Owner instructions",
    )

    def convert(schema: dict) -> dict:
        if not schema["x-convertible"]:
            raise ValueError("Unsupported schema")
        return schema

    monkeypatch.setattr(llm_projection, "convert_to_voluptuous", convert)
    api = native_llm.NativeLlmApi(
        hass=object(), id="ha_mcp_tools-entry-toolsearch", name="Tools", manager=manager
    )
    context = SimpleNamespace(
        context=SimpleNamespace(user_id="alice"), platform="fixture", language="en"
    )
    instance = await api.async_get_api_instance(context)
    assert {tool.name for tool in instance.tools} == {
        "converted",
        "ha_search_tools",
        "ha_call_tool",
    }
    search = next(tool for tool in instance.tools if tool.name == "ha_search_tools")
    result = await search.async_call(
        object(), SimpleNamespace(tool_args={"tools": ["unconverted"]}), context
    )
    data = result if isinstance(result, dict) else result.data
    assert data["results"][0]["input_schema"] == definitions[1].input_schema
    assert "Native owner" in data["results"][0]["description"]
    assert instance.api_prompt.startswith("Owner instructions")
    assert describe_tool(definitions[0]).title == "Converted"

    api.mode = "full"
    full = await api.async_get_api_instance(context)
    assert [tool.name for tool in full.tools] == ["converted"]
    assert full.tools[0].title == "Converted"
    assert full.api_prompt == "Owner instructions"
