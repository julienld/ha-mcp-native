"""Observe actual Core calls and runtime, without replacing any transport/tool."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import platform
import re
import sys
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as CORE_VERSION
from homeassistant.core import SupportsResponse

from .provenance import file_digest, tree_digest

DOMAIN = "protocol_fixture"
ENTITY = "sensor.protocol_fixture"


def _manager_entry(hass: Any, manager: Any) -> Any:
    # U2's EmbeddedServerManager has no public entry; NativeServerManager does.
    entry = (
        manager.entry if hasattr(manager, "entry") else getattr(manager, "_entry", None)
    )
    if (
        entry is None
        or hass.config_entries.async_get_entry(entry.entry_id) is not entry
    ):
        raise ValueError("runtime_entry_owner_mismatch")
    return entry


def runtime_proof(hass: Any) -> dict[str, Any]:
    import ha_mcp
    from custom_components import ha_mcp_tools

    receipt = json.loads(Path(hass.config.path("protocol-build.json")).read_bytes())
    component = Path(ha_mcp_tools.__file__).parent
    package = Path(ha_mcp.__file__).parent
    versions = {}
    for name in ("fastmcp", "mcp", "mcp_types"):
        marker = (package / "_vendor" / name / "VENDORED").read_text().splitlines()[0]
        versions[name] = marker.split("==", 1)[1]
    manager = hass.data.get("ha_mcp_tools", {}).get("manager")
    entry = _manager_entry(hass, manager)
    observed_runtime = "native" if hasattr(manager, "engine") else "legacy"
    component_hash = tree_digest(component)
    package_hash = tree_digest(package)
    return {
        "run_nonce": receipt["run_nonce"],
        "revision": receipt["revision"],
        "core_image_id": receipt["core_image_id"],
        "source_clean_at_receipt": receipt["source_clean_at_receipt"],
        "component_sha256": component_hash,
        "component_matches_build": component_hash == receipt["component_sha256"],
        "package_sha256": package_hash,
        "package_matches_build": package_hash == receipt["package_sha256"],
        "wheel_sha256": file_digest(Path(hass.config.path(receipt["wheel_name"]))),
        "core_version": CORE_VERSION,
        "python_version": platform.python_version(),
        "architecture": platform.machine(),
        "runtime": observed_runtime,
        "manager_type": type(manager).__name__,
        "running": bool(manager and manager.is_running),
        "ha_mcp_version": importlib.metadata.version("ha-mcp"),
        "private_versions": versions,
        "private_sdk_loaded": "ha_mcp._vendor.mcp" in sys.modules,
        "entry_runtime": entry.options.get("runtime_mode", "legacy"),
    }


class FixtureView(HomeAssistantView):
    url = "/api/protocol_fixture"
    name = "protocol_fixture:observe"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        if not request["hass_user"].is_admin:
            raise web.HTTPForbidden()
        hass = request.app["hass"]
        proof = await hass.async_add_executor_job(runtime_proof, hass)
        logging.getLogger(__name__).warning("PROTOCOL_COMPARISON_CAPTURE")
        state = hass.states.get(ENTITY)
        return web.json_response(
            {
                **proof,
                "state": state.state if state else None,
                "records": list(hass.data[DOMAIN]["records"]),
            }
        )

    async def post(self, request: web.Request) -> web.Response:
        if not request["hass_user"].is_admin:
            raise web.HTTPForbidden()
        body = await request.json()
        if set(body) != {"read_only"} or type(body["read_only"]) is not bool:
            raise web.HTTPBadRequest()
        hass = request.app["hass"]
        async with hass.data[DOMAIN]["lock"]:
            await set_read_only(hass, body["read_only"])
        return web.json_response({"read_only": body["read_only"]})


async def set_read_only(hass: Any, enabled: bool) -> None:
    """Use the real persisted policy and entry lifecycle, not an engine patch."""
    manager = hass.data["ha_mcp_tools"]["manager"]
    entry_id = _manager_entry(hass, manager).entry_id
    target = Path(hass.config.path(".ha_mcp/feature_flags.json"))

    def change() -> None:
        flags = json.loads(target.read_bytes()) if target.exists() else {}
        flags["read_only_mode"] = enabled
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(flags), encoding="utf-8")

    async with asyncio.timeout(120):
        if not await hass.config_entries.async_unload(entry_id):
            raise RuntimeError("fixture_entry_unload_failed")
        await hass.async_add_executor_job(change)
        if not await hass.config_entries.async_setup(entry_id):
            raise RuntimeError("fixture_entry_setup_failed")


async def setup(hass: Any) -> bool:
    # The runner also matches this unique nonce before allowing fixture writes.
    receipt = await hass.async_add_executor_job(
        lambda: json.loads(Path(hass.config.path("protocol-build.json")).read_bytes())
    )
    if not re.fullmatch(r"[a-f0-9]{32}", receipt.get("run_nonce", "")):
        raise ValueError("isolated_fixture_nonce_required")
    hass.data[DOMAIN] = {"records": [], "lock": asyncio.Lock()}
    hass.states.async_set(ENTITY, "ready", {"friendly_name": "Protocol fixture"})

    async def echo(call: Any) -> dict[str, Any]:
        marker = call.data.get("marker")
        if marker not in {"actor-a", "actor-b", "blocked"}:
            raise ValueError("fixture_marker_invalid")
        records = hass.data[DOMAIN]["records"]
        if len(records) >= 32:
            raise ValueError("fixture_record_limit")
        result = {
            "marker": marker,
            "actor": call.context.user_id,
            "nested": {"values": [0, False, ""], "empty": {}},
        }
        records.append(result)
        hass.states.async_set(ENTITY, marker, context=call.context)
        return result

    hass.services.async_register(
        DOMAIN, "echo", echo, supports_response=SupportsResponse.ONLY
    )
    hass.http.register_view(FixtureView())
    return True
