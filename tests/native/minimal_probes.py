"""Small real-runtime probes after Core has installed the minimal profile."""

import asyncio
import importlib
import io
import os
import socket
import sys
from importlib.abc import MetaPathFinder
from pathlib import Path
from typing import Any


class RejectSharedSDK(MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname.split(".", maxsplit=1)[0] in {"mcp", "mcp_types", "fastmcp"}:
            raise AssertionError(f"Minimal runtime imported shared SDK {fullname}")


async def _native(config: Path) -> None:
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.auth.const import GROUP_ID_ADMIN
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant(str(config))
    hass.auth = await auth_manager_from_config(hass, [], [])
    user = await hass.auth.async_create_user(
        "Minimal fixture", group_ids=[GROUP_ID_ADMIN]
    )
    module = await asyncio.to_thread(
        importlib.import_module, "custom_components.ha_mcp_tools.native_server"
    )
    await asyncio.to_thread(module._prewarm)
    from custom_components.ha_mcp_tools.native_backend import NativeBackend
    from ha_mcp.runtime import Engine
    from ha_mcp.runtime.catalog import build_initial_catalog
    from ha_mcp.tool_api import CallContext

    backend = NativeBackend(hass)
    engine = Engine(backend, build_initial_catalog(backend))
    try:
        hass.states.async_set("sensor.minimal_fixture", "ready")
        result = await engine.call(
            "ha_get_state",
            {"entity_id": "sensor.minimal_fixture"},
            CallContext(user.id),
        )
        assert not result.is_error and result.data["data"]["state"] == "ready"
    finally:
        await engine.stop()
        await backend.stop()
        await hass.async_stop()


async def _legacy_http() -> None:
    import uvicorn

    from custom_components.ha_mcp_tools import llm_api
    from ha_mcp._vendor.fastmcp import Context, FastMCP

    server = FastMCP("Minimal Core profile", dereference_schemas=True)

    @server.tool()
    async def echo(value: str, ctx: Context) -> dict[str, str]:
        await ctx.set_state("probe", value)
        return {"echo": await ctx.get_state("probe")}

    app = server.http_app(path="/mcp", host_origin_protection=False)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    runner = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ws="none",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    )
    task = asyncio.create_task(runner.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not runner.started:
                if task.done():
                    task.result()
                    raise RuntimeError("The fixture listener stopped before readiness")
                await asyncio.sleep(0.01)
        await asyncio.to_thread(llm_api._import_mcp_sdk)
        async with llm_api._mcp_session(f"http://127.0.0.1:{port}/mcp") as (
            session,
            _init,
        ):
            listed = await session.list_tools()
            assert "echo" in {tool.name for tool in listed.tools}
            result = await session.call_tool("echo", {"value": "minimal"})
            wire = result.model_dump(by_alias=True)
            assert not wire["isError"] and wire["structuredContent"] == {
                "echo": "minimal"
            }
        assert "key_value.aio.stores.memory" in sys.modules
    finally:
        runner.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            listener.close()


async def run_probes(config: Path) -> list[str]:
    os.environ["HA_MCP_CONFIG_DIR"] = str(config / ".ha_mcp")
    sys.path.insert(0, str(config))
    guard = RejectSharedSDK()
    sys.meta_path.insert(0, guard)
    try:
        import jsonref

        import ha_mcp

        assert await asyncio.to_thread(
            lambda: (
                Path(ha_mcp.__file__)
                .resolve()
                .is_relative_to(Path(sys.prefix).resolve())
            )
        )
        assert not any(
            name.split(".")[0] in {"mcp", "mcp_types", "fastmcp"}
            for name in sys.modules
        )
        references = jsonref.replace_refs(
            {"source": {"type": "string"}, "value": {"$ref": "#/source"}}
        )
        assert references["value"]["type"] == "string"
        from ha_mcp._vendor.rich.console import Console
        from ha_mcp._vendor.rich.markdown import Markdown

        text = io.StringIO()
        console = Console(file=text, force_terminal=False)
        console.print(Markdown("```python\nvalue = 1\n```"))
        try:
            raise ValueError("minimal traceback fixture")
        except ValueError:
            console.print_exception(show_locals=False)
        assert "minimal traceback fixture" in text.getvalue()
        await _native(config)
        await _legacy_http()
        return [
            "installed_wheel_imported_from_isolated_venv",
            "private_Rich_markdown_and_traceback",
            "JSON_schema_reference_resolution",
            "native_prewarm_and_Engine_read_with_real_Core_actor_and_state",
            "FastMCP_HTTP_app_and_real_private_Legacy_client",
            "lazy_memory_store_and_tool_result_envelope",
            "no_shared_MCP_or_FastMCP_import",
        ]
    finally:
        sys.meta_path.remove(guard)
