"""Executed in a separate client process using the image's unchanged Core SDK."""

import importlib.metadata
import json
import os
import sys


async def probe() -> None:
    parameters = json.load(sys.stdin)
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    assert importlib.metadata.version("mcp").startswith("1.")
    async with (
        streamablehttp_client(
            parameters["url"],
            headers={"Authorization": "Bearer " + parameters["token"]},
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()
        assert any(tool.name == "ha_get_state" for tool in tools.tools)
        state = await session.call_tool(
            "ha_get_state", {"entity_id": "sensor.native_fixture"}
        )
        assert not state.isError and "ready" in state.model_dump_json()
    print(
        "PASS shared_Core_SDK1_streamable_client",
        initialized.protocolVersion,
        flush=True,
    )
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
