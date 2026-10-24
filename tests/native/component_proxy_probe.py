"""Run the standard MCP compatibility bridge outside the Home Assistant process."""

import asyncio
import json
import re
import sys


async def probe() -> None:
    from ha_mcp._vendor.fastmcp import Client
    from ha_mcp.native_proxy import create_native_component_proxy

    parameters = json.load(sys.stdin)
    proxy = create_native_component_proxy(parameters["url"], auth=parameters["token"])
    async with Client(proxy) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert tools["ha_get_state"].annotations.read_only_hint is True
        assert tools["ha_config_list_helpers"].annotations.read_only_hint is False
        state = await client.call_tool(
            "ha_get_state", {"entity_id": "sensor.native_fixture"}
        )
        assert state.data["data"]["state"] == "ready", state
        guide = await client.call_tool("ha_get_skill_guide")
        key = re.search(r"Acknowledgment key: ([^\s]+)", guide.data["content"])
        assert key, guide
        created = await client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "Native bridge probe",
                "BestPracticeKey": key[1],
            },
        )
        assert created.data["success"], created
        state = await client.call_tool(
            "ha_get_state", {"entity_id": created.data["entity_id"]}
        )
        assert state.data["data"]["state"] == "off", state
        resources = await client.list_resources()
        assert any(str(resource.uri).startswith("skill://") for resource in resources)
    print(
        "PASS standard_MCP_bridge_calls_canonical_Core_and_relays_catalog_resources",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(probe())
