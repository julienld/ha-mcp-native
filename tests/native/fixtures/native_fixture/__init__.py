"""Disposable users and inspection endpoints for the native migration CI."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import secrets
import socket
import sys
import threading
from importlib.abc import MetaPathFinder
from pathlib import Path
from typing import Any

import probatio
import psutil
from aiohttp import web
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import Platform
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
            self,
            "Use the fixture's own validator.",
            llm_context,
            [EchoTool(), TurnOnTool()],
        )


class TurnOnTool(llm.Tool):
    name = "NativeTurnOn"
    description = "Turn on the disposable fixture light through Core's service owner."
    parameters = probatio.from_openapi(
        {"type": "object", "properties": {}, "additionalProperties": False}
    )
    annotations = llm.ToolAnnotations(
        read_only=False, destructive=False, idempotent=True
    )

    async def async_call(self, hass, tool_input, llm_context):
        self.parameters(tool_input.tool_args)
        await hass.services.async_call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.native_light"},
            blocking=True,
            context=llm_context.context,
        )
        return llm.ToolResult(data={"turned_on": True})


class FixtureView(HomeAssistantView):
    url = "/api/native_fixture/{operation}"
    name = "native_fixture:control"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, operation):
        if not request["hass_user"].is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        observed = await self._observation(request, operation)
        if observed is not None:
            return observed
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
                "remote_import_sites": self.hass.data[DOMAIN]["remote_import_sites"],
                "owned_tasks": len(manager.engine.tasks)
                if getattr(manager, "engine", None)
                else None,
                "loopback_attempts": self.hass.data[DOMAIN]["loopback_attempts"],
                "ticks": self.hass.data[DOMAIN]["ticks"],
                "llm_apis": [api.id for api in llm.async_get_apis(self.hass)],
            }
        )

    async def _observation(
        self, request: web.Request, operation: str
    ) -> web.Response | None:
        observed = self._configuration_observation(request, operation)
        if observed is not None:
            return observed
        if operation == "energy_probe":
            from homeassistant.components.energy.data import async_get_manager

            owner = await async_get_manager(self.hass)
            return web.json_response({"data": owner.data})
        if operation == "dashboard_probe":
            from .dashboards import dashboard_probe

            return web.json_response(await dashboard_probe(self.hass))
        if operation == "event":
            return web.json_response(
                {
                    "events": self.hass.data[DOMAIN]["events"],
                    "request_user_id": request["hass_user"].id,
                }
            )
        if operation == "camera":
            from .camera import describe_camera

            return web.json_response(describe_camera(self.hass))
        if operation == "device":
            data = self.hass.data[DOMAIN]
            return web.json_response(
                {
                    "device_id": data.get("device_id"),
                    "device_removals": data.get("device_removals", 0),
                }
            )
        if operation == "entity":
            from .sensor import describe_sensor

            return web.json_response(
                describe_sensor(self.hass, self.hass.data[DOMAIN]["sensor_entry_id"])
            )
        return None

    def _configuration_observation(
        self, request: web.Request, operation: str
    ) -> web.Response | None:
        if operation == "update_probe":
            from .update_actions import update_probe

            return web.json_response(update_probe(self.hass))
        if operation == "pipeline_probe":
            from .pipeline_support import pipeline_probe

            return web.json_response(pipeline_probe(self.hass))
        if operation == "system_actions":
            from .system_actions import system_actions_probe

            return web.json_response(system_actions_probe(self.hass))
        if operation == "themes":
            from .themes import theme_probe

            return web.json_response(theme_probe(self.hass))
        if operation == "group_probe":
            from .groups import describe_group

            data = describe_group(self.hass)
            data["request_user_id"] = request["hass_user"].id
            return web.json_response(data)
        return None

    async def post(self, request, operation):
        user = request["hass_user"]
        if not user.is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        body = await request.json()
        manager = self.hass.data.get("ha_mcp_tools", {}).get("manager")
        if operation == "guard":
            self.hass.data[DOMAIN]["guard"] = True
        elif operation == "pipeline_cleanup":
            from .pipeline_support import cleanup_pipeline

            await cleanup_pipeline(
                self.hass, body["pipeline_id"], body["preferred_pipeline"]
            )
        elif operation == "stage_system_script":
            from .system_actions import stage_system_script

            return web.json_response(await stage_system_script(self.hass, body["name"]))
        elif operation in {
            "seed_history",
            "seed_logs",
            "seed_entity_history",
            "seed_health",
            "seed_default_dashboard",
            "seed_bulk",
            "seed_system_actions",
            "pipeline_seed",
            "seed_updates",
        }:
            return web.json_response(await self._seed_data(operation, manager))
        elif operation == "seed_resources":
            from homeassistant.components.lovelace.const import LOVELACE_DATA

            resources = self.hass.data[LOVELACE_DATA].resources
            assert not resources.loaded
            await resources.ll_config.async_save(
                {
                    "views": [],
                    "resources": [{"url": "/local/native-legacy.js", "type": "module"}],
                }
            )
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
            return await self._llm(user, manager, body)
        else:
            return web.json_response({"error": "unknown_operation"}, status=404)
        return web.json_response({"ok": True})

    async def _llm(self, user: Any, manager: Any, body: dict[str, Any]) -> web.Response:
        context = llm.LLMContext(
            platform=DOMAIN,
            context=Context() if body.get("anonymous") else Context(user_id=user.id),
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
            return web.json_response({"error": "admin_identity_required"}, status=403)
        return web.json_response({"data": result.data, "error": result.error})

    async def _seed_data(self, operation: str, manager: Any) -> dict[str, Any]:
        if operation == "seed_updates":
            from .update_actions import seed_updates

            return await seed_updates(self.hass)
        if operation == "pipeline_seed":
            from .pipeline_support import seed_pipeline_fixture

            seed_pipeline_fixture(self.hass)
            return {}
        if operation == "seed_system_actions":
            from .system_actions import seed_system_actions

            return await seed_system_actions(self.hass)
        if operation == "seed_bulk":
            from .bulk import seed_bulk

            return await seed_bulk(self.hass)
        if operation == "seed_default_dashboard":
            from .dashboards import seed_default_dashboard

            return await seed_default_dashboard(self.hass)
        if operation == "seed_health":
            from .system_health import seed_health

            return await seed_health(self.hass)
        if operation == "seed_entity_history":
            from .sensor import seed_entity_state_history

            return await seed_entity_state_history(self.hass, manager)
        if operation == "seed_logs":
            from .logs import seed_logs

            return await seed_logs(self.hass)
        from .history import seed_history

        return await seed_history(self.hass)


async def async_setup(hass, config):
    data: dict[str, Any] = {
        "ticks": 0,
        "guard": False,
        "loopback_attempts": 0,
        "remote_import_sites": [],
    }
    hass.data[DOMAIN] = data
    from .events import observe_events

    observe_events(hass)
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

    class ImportTrace(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "ha_mcp.client":
                frame = sys._getframe().f_back
                stack = []
                while frame is not None:
                    filename = frame.f_code.co_filename
                    if "ha_mcp" in filename:
                        stack.append(
                            f"{Path(filename).name}:{frame.f_lineno}:{frame.f_code.co_name}"
                        )
                    frame = frame.f_back
                data["remote_import_sites"].append(stack)

    sys.meta_path.insert(0, ImportTrace())
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
    hass.states.async_set(
        "sensor.native_energy",
        "100",
        {
            "friendly_name": "Native energy source",
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
        },
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


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:native-device")},
        name=entry.title,
    )
    hass.data[DOMAIN]["device_id"] = device.id
    hass.data[DOMAIN]["sensor_entry_id"] = entry.entry_id
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    lifecycle = (
        hass.data[DOMAIN]
        .setdefault("entry_lifecycle", {})
        .setdefault(entry.entry_id, {"setup": 0, "unload": 0})
    )
    lifecycle["setup"] += 1
    return True


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform.SENSOR]
    )
    if unloaded:
        lifecycle = (
            hass.data[DOMAIN]
            .setdefault("entry_lifecycle", {})
            .setdefault(entry.entry_id, {"setup": 0, "unload": 0})
        )
        lifecycle["unload"] += 1
    return unloaded


async def async_remove_config_entry_device(hass: Any, entry: Any, device: Any) -> bool:
    data = hass.data[DOMAIN]
    data["device_removals"] = data.get("device_removals", 0) + 1
    await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
    return True
