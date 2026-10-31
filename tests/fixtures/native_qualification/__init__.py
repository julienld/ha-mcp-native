"""Read-only runtime attestation installed only in disposable Core tests."""

from __future__ import annotations

import json
import platform
import sys
import threading
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import HomeAssistant


class RuntimeProbe(HomeAssistantView):
    url = "/api/native_qualification"
    name = "native_qualification:runtime"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        from custom_components.ha_mcp_tools.runtime_generation import (
            native_bound_version,
        )

        if not request["hass_user"].is_admin:
            raise web.HTTPForbidden()
        hass = request.app["hass"]
        manager = hass.data.get("ha_mcp_tools", {}).get("manager")
        task = getattr(manager, "_supervisor", None)
        entry = getattr(manager, "entry", None)
        proof = await hass.async_add_executor_job(
            lambda: json.loads(
                Path(hass.config.path("native-build.json")).read_text(encoding="utf-8")
            )
        )
        observed = {
            "core_version": CORE_VERSION,
            "python_version": sys.version,
            "architecture": platform.machine(),
            "entry_id": entry.entry_id if entry else None,
            "manager": type(manager).__name__,
            "runtime": entry.options.get("runtime_mode") if entry else None,
            "running": bool(manager and manager.is_running),
            "on_core_loop": task is not None and task.get_loop() is hass.loop,
            "legacy_listener": hasattr(manager, "port"),
            "legacy_thread": any(
                thread.name == "ha-mcp-server" for thread in threading.enumerate()
            ),
            "admin_token_present": bool(entry and entry.data.get("admin_token")),
            "library_version": native_bound_version(),
            "revision": proof["revision"],
        }
        if request.query.get("llm") == "1":
            from homeassistant.helpers import llm

            from custom_components.ha_mcp_tools.llm_bridge_options import export_enabled

            prefix = f"ha_mcp_tools-{entry.entry_id}" if entry else None
            observed["llm_api_export_enabled"] = bool(
                entry and export_enabled(entry.options)
            )
            apis = [
                api
                for api in llm.async_get_apis(hass)
                if prefix and api.id in {prefix, prefix + "-toolsearch"}
            ]
            observed["llm_api_ids"] = [api.id for api in apis]
            observed["llm_api_types"] = [type(api).__name__ for api in apis]
        return self.json(observed)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    hass.http.register_view(RuntimeProbe())
    return True
