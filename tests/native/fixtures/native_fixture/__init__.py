"""Disposable users and inspection endpoints for the native migration CI."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import secrets
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import probatio
import psutil
from aiohttp import web
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Context
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

DOMAIN = "native_fixture"


class EchoTool(llm.Tool):
    name = "NativeEcho"
    description = "Echo validated text and the original caller."
    parameters = probatio.from_openapi(
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
    )
    annotations = llm.ToolAnnotations(
        read_only=True, destructive=False, idempotent=True
    )

    async def async_call(self, hass, tool_input, llm_context):
        arguments = self.parameters(tool_input.tool_args)
        return llm.ToolResult(
            data={
                "text": arguments["text"].upper(),
                "user_id": llm_context.context.user_id,
            }
        )


class FixtureAPI(llm.API):
    async def async_get_api_instance(self, llm_context):
        return llm.APIInstance(
            self, "Use the fixture's own validator.", llm_context, [EchoTool()]
        )


class FixtureView(HomeAssistantView):
    url = "/api/native_fixture/{operation}"
    name = "native_fixture:control"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, operation):
        if not request["hass_user"].is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        manager = self.hass.data.get("ha_mcp_tools", {}).get("manager")
        connections = await self.hass.async_add_executor_job(
            lambda: psutil.Process().net_connections(kind="inet")
        )
        versions = await self.hass.async_add_executor_job(
            lambda: {
                name: importlib.metadata.version(name)
                for name in ("homeassistant", "mcp", "ha-mcp", "pydantic")
            }
        )
        dependencies = await self.hass.async_add_executor_job(
            lambda: json.loads(Path("/opt/native-dependencies.json").read_text())
        )
        return web.json_response(
            {
                "ready": bool(manager and manager.is_running),
                "manager_type": type(manager).__name__,
                "entry_id": manager.entry.entry_id if manager else None,
                "entry_data_keys": sorted(manager.entry.data) if manager else [],
                "path": getattr(manager, "path", None),
                "versions": versions,
                "dependencies": dependencies,
                "listeners": sorted(
                    {c.laddr.port for c in connections if c.status == "LISTEN"}
                ),
                "server_threads": [
                    t.name for t in threading.enumerate() if "ha-mcp-server" in t.name
                ],
                "remote_imports": [
                    name
                    for name in sys.modules
                    if name.startswith(("ha_mcp.client", "ha_mcp.__main__"))
                ],
                "owned_tasks": len(manager.engine.tasks)
                if getattr(manager, "engine", None)
                else None,
                "loopback_attempts": self.hass.data[DOMAIN]["loopback_attempts"],
                "ticks": self.hass.data[DOMAIN]["ticks"],
                "llm_apis": [api.id for api in llm.async_get_apis(self.hass)],
            }
        )

    async def post(self, request, operation):
        user = request["hass_user"]
        if not user.is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        body = await request.json()
        manager = self.hass.data.get("ha_mcp_tools", {}).get("manager")
        if operation == "guard":
            self.hass.data[DOMAIN]["guard"] = True
        elif operation == "configure":
            entry = manager.entry
            await self.hass.config_entries.async_unload(entry.entry_id)
            self.hass.config_entries.async_update_entry(
                entry,
                options={
                    **entry.options,
                    "llm_api_exposure": "both",
                    "native_import_apis": [DOMAIN],
                },
            )
            await self.hass.config_entries.async_setup(entry.entry_id)
        elif operation == "reload":
            await self.hass.config_entries.async_reload(manager.entry.entry_id)
        elif operation == "llm":
            context = llm.LLMContext(
                platform=DOMAIN,
                context=Context()
                if body.get("anonymous")
                else Context(user_id=user.id),
                language="en",
                assistant="conversation",
                device_id=None,
            )
            try:
                api = await llm.async_get_api(
                    self.hass, f"ha_mcp_tools-{manager.entry.entry_id}", context
                )
                result = await api.async_call_tool(
                    llm.ToolInput(
                        tool_name=body["tool_name"], tool_args=body.get("arguments", {})
                    )
                )
            except PermissionError:
                return web.json_response(
                    {"error": "admin_identity_required"}, status=403
                )
            return web.json_response({"data": result.data, "error": result.error})
        else:
            return web.json_response({"error": "unknown_operation"}, status=404)
        return web.json_response({"ok": True})


async def async_setup(hass, config):
    data: dict[str, Any] = {"ticks": 0, "guard": False, "loopback_attempts": 0}
    hass.data[DOMAIN] = data
    original_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if (
            data["guard"]
            and isinstance(address, tuple)
            and len(address) > 1
            and address[0] in ("127.0.0.1", "::1", "localhost")
            and address[1] in (8123, 9584)
        ):
            data["loopback_attempts"] += 1
            raise RuntimeError("native_HA_loopback_forbidden")
        return original_connect(sock, address)

    socket.socket.connect = guarded_connect
    provider = hass.auth.get_auth_provider("homeassistant", None)
    users = {}
    for name, group in (
        ("alpha", GROUP_ID_ADMIN),
        ("beta", GROUP_ID_ADMIN),
        ("reader", GROUP_ID_USER),
    ):
        password = secrets.token_urlsafe(24)
        await provider.async_add_auth(name, password)
        credential = await provider.async_get_or_create_credentials({"username": name})
        user = await hass.auth.async_create_user(name, group_ids=[group])
        await hass.auth.async_link_user(user, credential)
        users[name] = {"username": name, "password": password, "user_id": user.id}
    await async_setup_component(hass, "mcp_server", {})
    llm.async_register_api(
        hass, FixtureAPI(hass=hass, id=DOMAIN, name="Separate native fixture API")
    )
    hass.http.register_view(FixtureView(hass))
    hass.states.async_set(
        "sensor.native_fixture", "ready", {"friendly_name": "Native fixture"}
    )

    async def heartbeat():
        while True:
            data["ticks"] += 1
            await asyncio.sleep(0.05)

    hass.async_create_background_task(heartbeat(), "native-fixture-heartbeat")
    await hass.async_add_executor_job(
        Path(hass.config.path("test-identities.json")).write_text,
        json.dumps(users),
        "utf-8",
    )
    return True
