"""Executed in a separate client process using the image's unchanged Core SDK."""

import asyncio
import importlib.metadata
import json
import os
import sys


async def exercise_core_client(parameters: dict) -> None:
    from mcp import ClientSession

    transport = importlib.import_module("mcp.client.streamable_http")
    sdk_version = importlib.metadata.version("mcp")
    major = sdk_version.split(".", 1)[0]
    assert major in {"1", "2"}, f"Unqualified Core MCP SDK {sdk_version}"
    http_lib = getattr(transport, "httpx2", None) or getattr(transport, "httpx", None)
    assert http_lib is not None, (
        "Core SDK transport does not expose its HTTP client owner"
    )
    async with (
        http_lib.AsyncClient(
            headers={"Authorization": "Bearer " + parameters["token"]},
            timeout=http_lib.Timeout(30, read=300),
            follow_redirects=True,
        ) as http_client,
        transport.streamable_http_client(
            parameters["url"], http_client=http_client
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()
        assert any(tool.name == "ha_get_state" for tool in tools.tools)
        state = await session.call_tool(
            "ha_get_state", {"entity_id": "sensor.native_fixture"}
        )
        failed = state.isError if major == "1" else state.is_error
        assert not failed and "ready" in state.model_dump_json()
        protocol = (
            initialized.protocolVersion
            if major == "1"
            else initialized.protocol_version
        )
    print("PASS shared_Core_SDK_streamable_client", sdk_version, protocol, flush=True)


async def probe() -> None:
    parameters = json.load(sys.stdin)
    await exercise_core_client(parameters)
    os.environ.update(
        {
            "HOMEASSISTANT_URL": parameters["base_url"],
            "HOMEASSISTANT_TOKEN": parameters["token"],
            "ENABLE_AUTO_BACKUP": "false",
            "ENABLE_MANDATORY_BPS": "false",
            "ENABLE_STRICT_MANDATORY_BPS": "false",
        }
    )
    from ha_mcp.backends.remote import RemoteBackend
    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.runtime import Engine
    from ha_mcp.runtime.catalog import build_initial_catalog
    from ha_mcp.tool_api import CallContext

    remote = HomeAssistantClient(
        base_url=parameters["base_url"], token=parameters["token"]
    )
    backend = RemoteBackend(remote)
    engine = Engine(backend, build_initial_catalog(backend))
    actor = CallContext(parameters["user_id"], client_id="remote-ci")
    try:
        state = await engine.call(
            "ha_get_state", {"entity_id": "sensor.native_fixture"}, actor
        )
        assert not state.is_error and "ready" in json.dumps(state.data)
        services = await engine.call(
            "ha_list_services", {"domain": "input_boolean"}, actor
        )
        assert not services.is_error and "input_boolean" in json.dumps(services.data)
        created = await engine.call(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "Remote backend probe",
                "config": {"initial": False},
                "MandatoryBPS": False,
            },
            actor,
        )
        assert not created.is_error, created.data
        updated = await engine.call(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": created.data["data"]["id"],
                "name": "Remote backend probe updated",
                "MandatoryBPS": False,
            },
            actor,
        )
        assert not updated.is_error, updated.data
    finally:
        await engine.stop()
        await remote.close()
    print("PASS shared_tool_bodies_over_real_remote_REST_and_WebSocket", flush=True)


if __name__ == "__main__":
    asyncio.run(probe())
