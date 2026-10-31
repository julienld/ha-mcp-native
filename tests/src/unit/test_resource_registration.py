"""Keep dashboard resource discovery and safety metadata in the public registry."""

from unittest.mock import MagicMock

from ha_mcp.tools.tools_resources import ResourceTools, register_resources_tools


class TestToolRegistration:
    def test_registers_three_tools(self):
        mcp = MagicMock()
        registered = []
        mcp.add_tool = lambda method: registered.append(method.__name__)

        register_resources_tools(mcp, MagicMock())

        assert "ha_config_list_dashboard_resources" in registered
        assert "ha_config_set_dashboard_resource" in registered
        assert "ha_config_delete_dashboard_resource" in registered
        assert "ha_config_set_inline_dashboard_resource" not in registered
        assert len(registered) == 3

    def test_set_tool_has_destructive_hint(self):
        tools = ResourceTools(MagicMock())
        metadata = tools.ha_config_set_dashboard_resource.__fastmcp__
        assert metadata.annotations.destructive_hint is True
