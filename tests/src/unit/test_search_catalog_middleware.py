"""Our catalog override preserves FastMCP's inner middleware control."""

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.server.context import Context
from ha_mcp._vendor.fastmcp.server.middleware import Middleware
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp.tools.tool_hints import read_only_hints
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform


async def test_search_catalog_can_skip_middleware_that_already_ran():
    class AlreadyApplied(Middleware):
        async def on_list_tools(self, context, call_next):
            raise AssertionError("The outer request already applied this middleware")

    async def get_state():
        return "on"

    server = FastMCP("Catalog contract")
    server.add_tool(
        Tool.from_function(
            get_state,
            name="ha_get_state",
            annotations=read_only_hints("Get state", open_world=False),
        )
    )
    transform = CategorizedSearchTransform()
    server.add_transform(transform)
    server.add_middleware(AlreadyApplied())

    async with Context(server) as context:
        catalog = await transform.get_tool_catalog(context, run_middleware=False)

    assert [tool.name for tool in catalog] == ["ha_get_state"]
