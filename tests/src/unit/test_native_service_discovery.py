"""Native discovery describes the service path without promising raw WebSocket."""

from types import SimpleNamespace

from ha_mcp.runtime.catalog import build_initial_catalog
from ha_mcp.tool_api.llm_projection import describe_tool

from ._embedded_stubs import install
from .test_native_catalog_contract import _changed_inputs
from .test_native_engine_backend import native as native

install()

from custom_components.ha_mcp_tools.native_server import _project_tool  # noqa: E402


def test_native_mcp_and_assist_disclose_unsupported_ws_parameter(
    native: object,
) -> None:
    from custom_components.ha_mcp_tools.native_catalog import build_native_catalog

    manager = SimpleNamespace(
        backend=SimpleNamespace(_templates=None, hass=None, authorize=None),
        hass=None,
        snapshots=None,
    )
    original = next(
        tool
        for tool in build_initial_catalog(manager.backend)
        if tool.name == "ha_call_service"
    )
    definition = next(
        tool for tool in build_native_catalog(manager) if tool.name == "ha_call_service"
    )
    for projected in (_project_tool(manager, definition), describe_tool(definition)):
        assert "ws_command is not supported in native mode" in projected.description
        assert "WebSocket" not in projected.description.splitlines()[0]
        assert "**WebSocket command escape hatch" not in projected.description
        schema = getattr(projected, "parameters", None) or projected.input_schema
        assert (
            "not supported in native mode"
            in schema["properties"]["ws_command"]["description"].lower()
        )
        assert "raw command payload" not in schema["properties"]["data"]["description"]
        wait_help = schema["properties"]["wait"]["description"]
        assert "legacy" not in wait_help
        assert "each target" in wait_help and "comma-separated" in wait_help
        assert not _changed_inputs(original.input_schema, schema)
    assert definition.handler is not None
    assert definition.meta["ha_mcp"]["native_support"]["partial"] is True
    assert "send one" in original.description
    assert (
        "not supported in native mode"
        not in original.input_schema["properties"]["ws_command"]["description"]
    )
