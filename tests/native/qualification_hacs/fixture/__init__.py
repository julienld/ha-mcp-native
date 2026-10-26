"""Bench-only network redirection and inspection; never replaces an owner."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.start import async_at_started
from homeassistant.setup import async_setup_component
from yarl import URL

from .content import DOMAIN as DUMMY_DOMAIN
from .content import REPOSITORY_ID, ContentServer

DOMAIN = "qualification_hacs_fixture"
ENTRY_ID = "11111111111111111111111111111111"
HOSTS = {
    "api.github.com",
    "github.com",
    "raw.githubusercontent.com",
    "data-v2.hacs.xyz",
}


def disk_image(hass: HomeAssistant) -> dict[str, str]:
    paths = list(Path(hass.config.path(".storage")).glob("hacs.*"))
    paths += list(
        Path(hass.config.path("custom_components", DUMMY_DOMAIN)).rglob("*.py")
    )
    manifest = Path(
        hass.config.path("custom_components", DUMMY_DOMAIN, "manifest.json")
    )
    if manifest.exists():
        paths.append(manifest)
    return {
        str(path.relative_to(hass.config.config_dir)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(paths)
        if path.is_file()
    }


async def observe(hass: HomeAssistant) -> dict[str, Any]:
    data = hass.data[DOMAIN]
    owner = hass.data.get("hacs")
    repository = owner.repositories.get_by_id(REPOSITORY_ID) if owner else None
    from custom_components.ha_mcp_tools.native_operations.hacs_access import (
        repository_image,
    )

    entity_id = er.async_get(hass).async_get_entity_id("update", "hacs", REPOSITORY_ID)
    return {
        "core": importlib.metadata.version("homeassistant"),
        "hacs": str(owner.version) if owner else None,
        "owner_class": f"{type(owner).__module__}.{type(owner).__name__}",
        "ready": bool(
            owner
            and not owner.status.startup
            and not owner.system.disabled
            and str(owner.stage) == "running"
        ),
        "entry_state": hass.config_entries.async_get_entry(ENTRY_ID).state.value,
        "repository": repository_image(repository) if repository else None,
        "update_entity": entity_id,
        "update_state": hass.states.get(entity_id).as_dict()
        if entity_id and hass.states.get(entity_id)
        else None,
        "disk": await hass.async_add_executor_job(disk_image, hass),
        "network": list(data["network"].requests),
        "unexpected_network": list(data["network"].unexpected),
        "install_services": list(data["installs"]),
        "archive_entered": data["network"].archive_entered.is_set(),
        "dummy_running_version": hass.data.get(DUMMY_DOMAIN),
    }


class QualificationView(HomeAssistantView):
    url = "/api/qualification_hacs/{operation}"
    name = "qualification_hacs:control"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, operation: str) -> web.Response:
        if not request["hass_user"].is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        if operation != "probe":
            return web.json_response({"error": "unknown_operation"}, status=404)
        return self.json(await observe(self.hass))

    async def post(self, request: web.Request, operation: str) -> web.Response:
        if not request["hass_user"].is_admin:
            return web.json_response({"error": "admin_required"}, status=403)
        network = self.hass.data[DOMAIN]["network"]
        body = await request.json()
        if operation == "archive_fault":
            network.fail_archive = bool(body.get("corrupt", False))
            network.hold_archive = bool(body.get("hold", False))
            network.archive_entered.clear()
            network.release_archive.clear()
        elif operation == "release_archive":
            network.release_archive.set()
        elif operation == "load_dummy":
            assert await async_setup_component(self.hass, DUMMY_DOMAIN, {})
        elif operation == "reload_owner":
            assert await self.hass.config_entries.async_reload(ENTRY_ID)
        else:
            return web.json_response({"error": "unknown_operation"}, status=404)
        return self.json({"ok": True})


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    network = ContentServer()
    await network.start()
    data: dict[str, Any] = {"network": network, "installs": []}
    hass.data[DOMAIN] = data
    original = aiohttp.ClientSession._request

    async def routed(
        session: aiohttp.ClientSession, method: str, str_or_url: Any, **kwargs: Any
    ) -> Any:
        url = URL(str_or_url)
        if url.host in HOSTS:
            url = URL(network.base + "/" + url.host + url.raw_path_qs)
        elif url.host not in {"127.0.0.1", "::1", "localhost"}:
            network.unexpected.append(f"BLOCKED {method} {url.host}{url.path}")
            raise RuntimeError(
                "Qualification blocked an unmodelled external destination"
            )
        return await original(session, method, url, **kwargs)

    # Change transport destination only. Real aiohttp, aiogithubapi, HACS,
    # update.install, Core validators, file writes and rollback still run.
    aiohttp.ClientSession._request = routed

    @callback
    def record(event: Event) -> None:
        if (
            event.data.get("domain") == "update"
            and event.data.get("service") == "install"
        ):
            data["installs"].append(
                {
                    "data": dict(event.data["service_data"]),
                    "user_id": event.context.user_id,
                }
            )

    hass.bus.async_listen("call_service", record)
    hass.http.register_view(QualificationView(hass))

    async def started(_: HomeAssistant) -> None:
        entry = hass.config_entries.async_get_entry(ENTRY_ID)
        if entry.disabled_by is not None:
            assert await hass.config_entries.async_set_disabled_by(ENTRY_ID, None)
        elif entry.state.value != "loaded":
            assert await hass.config_entries.async_setup(ENTRY_ID)

    async_at_started(hass, started)

    async def stopped(_: Event) -> None:
        aiohttp.ClientSession._request = original
        await network.close()

    hass.bus.async_listen_once("homeassistant_stop", stopped)
    return True
