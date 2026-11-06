"""Empty overview collections remain present through projection."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_search import register_search_tools

from .test_overview_system_info import (
    component_probe_has_a_local_test_world as component_probe_has_a_local_test_world,
)


class TestHaGetOverviewAlwaysEmittedKeys:
    """Keys advertised in the ``fields=`` docstring must always be in
    the result so ``fields=[<key>]`` never trips the ``project_fields``
    typo-guard warning on a clean instance.

    Regression coverage for the LLM complaint that ``notifications`` /
    ``repairs`` were "not in available keys" on an HA instance with no
    active alerts — the docstring promised them but the code emitted
    them only when non-empty.
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def capture_add_tool(method):
            name = (
                method.__fastmcp__.name
                if hasattr(method, "__fastmcp__")
                else method.__name__
            )
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
        return mcp

    @pytest.fixture
    def mock_client_empty_ws(self):
        """Client whose WS calls all return success with empty lists."""
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})

        async def empty_ws(msg):
            if msg.get("type") == "persistent_notification/get":
                return {"success": True, "result": []}
            if msg.get("type") == "repairs/list_issues":
                return {"success": True, "result": {"issues": []}}
            return {"success": True, "result": []}

        client.send_websocket_message = AsyncMock(side_effect=empty_ws)
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(return_value={"success": True})
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client_empty_ws, mock_smart_tools):
        register_search_tools(
            mock_mcp, mock_client_empty_ws, smart_tools=mock_smart_tools
        )
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_notifications_emitted_as_empty_list_when_none(self, overview_tool):
        result = await overview_tool(detail_level="minimal")
        assert result["notifications"] == []
        assert result["notification_count"] == 0

    @pytest.mark.asyncio
    async def test_repairs_emitted_as_empty_list_when_none(self, overview_tool):
        result = await overview_tool(detail_level="minimal")
        assert result["repairs"] == []
        assert result["repair_count"] == 0

    @pytest.mark.asyncio
    async def test_fields_projection_returns_empty_lists_without_warning(
        self, overview_tool
    ):
        """``fields=["notifications","repairs"]`` on a clean instance must
        return both as empty lists, with no ``warnings`` entry complaining
        about missing keys.
        """
        result = await overview_tool(fields=["notifications", "repairs"])
        assert result["notifications"] == []
        assert result["repairs"] == []
        # project_fields() appends a "not found in response" warning when
        # the requested key is absent. The whole point of the empty-list
        # default is to keep this warning silent on a clean instance.
        warnings = result.get("warnings") or []
        joined = " ".join(str(w) for w in warnings)
        assert "notifications" not in joined
        assert "repairs" not in joined
