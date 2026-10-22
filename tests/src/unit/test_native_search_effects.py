"""Real MCP proxies classify native mixed tools after their authoritative validation."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import native_server  # noqa: E402
from custom_components.ha_mcp_tools.native_server_policy import (  # noqa: E402
    NativePolicy,
    catalogue_transform,
)
from ha_mcp._vendor.fastmcp import Client, FastMCP  # noqa: E402
from ha_mcp.runtime import Engine  # noqa: E402
from ha_mcp.runtime.catalog import define_tool  # noqa: E402
from ha_mcp.runtime.helper_listing import HelperListingTools, description_starts_flow  # noqa: E402
from ha_mcp.tool_api import CallContext, ToolDefinition  # noqa: E402
from ha_mcp.tools.tool_hints import write_hints  # noqa: E402


class Backend:
    def __init__(self) -> None:
        self.authorize = AsyncMock()
        self.operations = []

    async def execute(
        self, operation: str, arguments: dict, context: CallContext
    ) -> Any:
        self.operations.append(operation)
        if operation == "helpers.catalog":
            return {
                "success": True,
                "helper_type": arguments["helper_type"],
                "helpers": [],
                "count": 0,
            }
        if operation == "config.flow.settings":
            return {"redact_secrets": False}
        if operation == "helpers.storage.schemas":
            return {
                "types": {"counter": {"create": [{"name": "name", "type": "string"}]}}
            }
        if operation == "helpers.flow.types":
            return {"types": ["utility_meter"]}
        if operation == "config.flow.start":
            return {
                "flow_id": "test-flow",
                "type": "form",
                "step_id": "user",
                "last_step": True,
                "data_schema": [{"name": "name", "type": "string"}],
            }
        if operation in {"config.flow.abort", "helpers.translations"}:
            return {}
        raise AssertionError(operation)


def _manager(
    monkeypatch: pytest.MonkeyPatch, *, policy_read_only: bool, channel_read_only: bool
) -> tuple:
    backend = Backend()
    actor = CallContext("owner", read_only=channel_read_only)
    monkeypatch.setattr(native_server, "current_context", lambda: actor)
    runtime = native_server.NativeServerManager.__new__(
        native_server.NativeServerManager
    )
    runtime.policy = NativePolicy(
        read_only=policy_read_only,
        flags={"enable_tool_search": True},
        pinned=frozenset(),
    )
    definition = define_tool(
        backend,
        HelperListingTools,
        "ha_config_list_helpers",
        annotations=write_hints(
            "List Helpers", destructive=True, idempotent=False, open_world=False
        ),
        writes=description_starts_flow,
    )
    pure_write = ToolDefinition(
        "ha_example_write",
        "A write only tool",
        {"type": "object"},
        AsyncMock(return_value={"success": True}),
    )
    runtime.engine = Engine(
        backend, [definition, pure_write], read_only=policy_read_only
    )
    runtime.mcp = FastMCP("Native mixed effects")
    for tool in runtime.engine.tools.values():
        runtime.mcp.add_tool(native_server._project_tool(runtime, tool))
    runtime.mcp.add_transform(catalogue_transform(runtime))
    runtime._configure_search()
    return runtime, backend, actor


@pytest.mark.parametrize(
    "policy_read_only,channel_read_only", [(True, False), (False, True)]
)
async def test_native_read_proxy_lists_and_describes_storage_but_cannot_start_flows(
    monkeypatch: pytest.MonkeyPatch, policy_read_only: bool, channel_read_only: bool
) -> None:
    runtime, backend, _ = _manager(
        monkeypatch,
        policy_read_only=policy_read_only,
        channel_read_only=channel_read_only,
    )
    async with Client(runtime.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert names == {"ha_search_tools", "ha_call_read_tool"}
        discovered = await client.call_tool(
            "ha_search_tools", {"tools": ["ha_config_list_helpers"]}
        )
        assert discovered.data[0]["annotations"]["readOnlyHint"] is False
        assert "ha_call_read_tool" in discovered.data[0]["execute_via"]
        assert "ha_call_write_tool" not in discovered.data[0]["execute_via"]
        for arguments in (
            {"helper_type": "counter"},
            {"helper_type": "counter", "describe": True},
            {"helper_type": "utility_meter", "describe": "false"},
        ):
            result = await client.call_tool(
                "ha_call_read_tool",
                {"name": "ha_config_list_helpers", "arguments": arguments},
                raise_on_error=False,
            )
            assert not result.is_error, result
        for proxy, arguments in (
            ("ha_call_read_tool", {"helper_type": "utility_meter", "describe": True}),
            (
                "ha_call_read_tool",
                {"helper_type": "counter", "describe": "not a boolean"},
            ),
            ("ha_call_write_tool", {"helper_type": "utility_meter", "describe": True}),
        ):
            refused = await client.call_tool(
                proxy,
                {"name": "ha_config_list_helpers", "arguments": arguments},
                raise_on_error=False,
            )
            assert refused.is_error
        assert "config.flow.start" not in backend.operations
        hidden = await client.call_tool(
            "ha_search_tools", {"tools": ["ha_example_write"]}, raise_on_error=False
        )
        assert hidden.is_error


async def test_native_nonreadonly_proxy_exposes_both_routes_and_only_write_proxy_can_probe_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, backend, _ = _manager(
        monkeypatch, policy_read_only=False, channel_read_only=False
    )
    async with Client(runtime.mcp) as client:
        found = await client.call_tool(
            "ha_search_tools", {"tools": ["ha_config_list_helpers"]}
        )
        assert "ha_call_read_tool" in found.data[0]["execute_via"]
        assert "ha_call_write_tool" in found.data[0]["execute_via"]
        refused = await client.call_tool(
            "ha_call_read_tool",
            {
                "name": "ha_config_list_helpers",
                "arguments": {"helper_type": "utility_meter", "describe": True},
            },
            raise_on_error=False,
        )
        assert refused.is_error and "config.flow.start" not in backend.operations
        allowed = await client.call_tool(
            "ha_call_write_tool",
            {
                "name": "ha_config_list_helpers",
                "arguments": {"helper_type": "utility_meter", "describe": True},
            },
        )
        assert not allowed.is_error
        assert backend.operations.count("config.flow.start") == 1
        assert backend.operations.count("config.flow.abort") == 1


async def test_cached_read_proxy_keeps_integration_metadata_but_rechecks_flow_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.runtime.catalog import _integration_reader

    runtime, backend, _ = _manager(
        monkeypatch, policy_read_only=True, channel_read_only=False
    )
    backend.redaction_enabled = lambda: False
    original = backend.execute

    async def read(operation: str, arguments: dict, context: CallContext) -> Any:
        if operation == "integrations.entries":
            return {
                "entries": [
                    {
                        "entry_id": "entry",
                        "domain": "demo",
                        "title": "Demo",
                        "options": {"count": 2},
                        "supports_options": True,
                    }
                ]
            }
        if operation == "integrations.log_levels":
            return {"success": True, "result": []}
        return await original(operation, arguments, context)

    backend.execute = read
    definition = _integration_reader(backend)
    runtime.engine.tools[definition.name] = definition
    runtime.mcp.add_tool(native_server._project_tool(runtime, definition))
    async with Client(runtime.mcp) as client:
        for arguments in ({}, {"domain": "demo"}, {"entry_id": "entry"}):
            result = await client.call_tool(
                "ha_call_read_tool",
                {"name": "ha_get_integration", "arguments": arguments},
                raise_on_error=False,
            )
            assert not result.is_error, result
        for arguments in (
            {"entry_id": "entry", "include_schema": "true"},
            {
                "entry_id": "entry",
                "include_subentry_schema": True,
                "subentry_type": "sensor",
            },
        ):
            denied = await client.call_tool(
                "ha_call_read_tool",
                {"name": "ha_get_integration", "arguments": arguments},
                raise_on_error=False,
            )
            assert denied.is_error
        backend.redaction_enabled = lambda: True
        denied = await client.call_tool(
            "ha_call_read_tool",
            {"name": "ha_get_integration", "arguments": {"entry_id": "entry"}},
            raise_on_error=False,
        )
        assert denied.is_error
    assert "config.flow.start" not in backend.operations
