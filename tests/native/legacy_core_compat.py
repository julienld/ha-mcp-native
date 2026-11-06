"""Real Legacy component/Core contracts, run in a disposable isolated Core venv."""

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .minimal_probes import RejectSharedSDK, _bootstrap_core
from .minimal_support import save


class CoreDiagnostics(logging.Handler):
    """Retain attributed API/blocking warnings instead of suppressing them."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.failures: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        if any(owner in message for owner in ("ha_mcp_tools", "ha_mcp/", "ha_mcp\\")):
            if "deprecated" in message.lower() or "blocking call" in message.lower():
                self.failures.append(message)


async def wait_ready(hass: Any) -> Any:
    from custom_components.ha_mcp_tools.const import (
        DATA_LLM_API_UNSUB,
        DATA_MANAGER,
        DOMAIN,
    )

    async with asyncio.timeout(180):
        while True:
            state = hass.data.get(DOMAIN, {})
            manager = state.get(DATA_MANAGER)
            if (
                manager is not None
                and manager.is_running
                and state.get(DATA_LLM_API_UNSUB)
            ):
                return manager
            await asyncio.sleep(0.1)


async def setup_entry(hass: Any, phase: str) -> Any:
    from homeassistant.auth.const import GROUP_ID_ADMIN
    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN

    from custom_components.ha_mcp_tools.const import (
        DOMAIN,
        EXPOSURE_FULL,
        OPT_ENABLE_LLM_API,
        OPT_ENABLE_SIDEBAR_PANEL,
        OPT_ENABLE_STARTUP_NOTIFICATION,
        OPT_LLM_API_EXPOSURE,
        OPT_SERVER_URL,
        OPT_WEBHOOK_AUTH,
        WEBHOOK_AUTH_NONE,
    )

    if phase == "full_restart":
        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        assert await hass.config_entries.async_setup(entries[0].entry_id)
        return entries[0]
    assert not hass.config_entries.async_entries(DOMAIN)
    user = await hass.auth.async_create_user(
        "Legacy Core fixture", group_ids=[GROUP_ID_ADMIN]
    )
    refresh = await hass.auth.async_create_refresh_token(
        user,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        client_name="Legacy qualification",
    )
    token = hass.auth.async_create_access_token(refresh)
    manager = hass.config_entries.flow
    flow = await manager.async_init(DOMAIN, context={"source": "user"})
    form = await manager.async_configure(flow["flow_id"], {"next_step_id": "server"})
    assert form["type"] == "form" and form["step_id"] == "server"
    created = await manager.async_configure(
        flow["flow_id"], {"admin_token": token, "remote_access": WEBHOOK_AUTH_NONE}
    )
    assert created["type"] == "create_entry", created
    entry = created["result"]
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            OPT_ENABLE_LLM_API: True,
            OPT_LLM_API_EXPOSURE: EXPOSURE_FULL,
            OPT_ENABLE_SIDEBAR_PANEL: False,
            OPT_ENABLE_STARTUP_NOTIFICATION: False,
            OPT_SERVER_URL: "http://127.0.0.1:8123",
            OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_NONE,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry


async def _assist_read(hass: Any, entry: Any) -> None:
    from homeassistant.core import Context
    from homeassistant.helpers import llm
    from homeassistant.requirements import async_get_integration_with_requirements

    from custom_components.ha_mcp_tools.const import DOMAIN

    await async_get_integration_with_requirements(hass, "conversation")
    api = next(
        api
        for api in llm.async_get_apis(hass)
        if api.id == f"{DOMAIN}-{entry.entry_id}"
    )
    context = llm.LLMContext(
        platform=DOMAIN,
        context=Context(),
        language="en",
        assistant="conversation",
        device_id=None,
    )
    instance = await api.async_get_api_instance(context)
    tool_result = await instance.async_call_tool(
        llm.ToolInput("ha_get_state", {"entity_id": "sensor.legacy_core_fixture"})
    )
    assert isinstance(tool_result, dict) and "ready" in json.dumps(tool_result)
    assert not tool_result.get("isError")


async def exercise(config: Path, phase: str) -> dict[str, Any]:
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.const import __version__ as core_version
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import llm
    from homeassistant.setup import async_setup_component

    from custom_components.ha_mcp_tools import embedded_setup, llm_api
    from custom_components.ha_mcp_tools.const import (
        DATA_ADMIN_TOKEN,
        DATA_DCR_SIGNING_KEY,
        DATA_OAUTH_CLIENT_ID,
        DATA_OAUTH_CLIENT_SECRET,
        DATA_OAUTH_SIGNING_KEY,
        DATA_SECRET_PATH,
        DATA_WEBHOOK_ID,
        DOMAIN,
        OPT_RUNTIME_MODE,
        OPT_WEBHOOK_AUTH,
        RUNTIME_LEGACY,
        RUNTIME_NATIVE,
    )

    assert not hasattr(llm, "ToolResult") and not hasattr(llm, "ToolAnnotations")
    hass = HomeAssistant(str(config))
    try:
        await _bootstrap_core(hass)
        hass.auth = await auth_manager_from_config(hass, [], [])
        http_config = {"http": {"server_host": "127.0.0.1", "server_port": 8123}}
        for domain in ("http", "api", "websocket_api"):
            assert await async_setup_component(hass, domain, http_config), domain
        entry = await setup_entry(hass, phase)
        await hass.async_start()
        manager = await wait_ready(hass)
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(hass, DOMAIN)
        paired_version = await asyncio.to_thread(importlib.metadata.version, "ha-mcp")
        assert integration.version == paired_version
        import ha_mcp

        assert await asyncio.to_thread(
            lambda: (
                Path(ha_mcp.__file__)
                .resolve()
                .is_relative_to(Path(sys.prefix).resolve())
            )
        )
        assert entry.options[OPT_RUNTIME_MODE] == RUNTIME_LEGACY
        try:
            embedded_setup._server_manager(
                hass, SimpleNamespace(options={OPT_RUNTIME_MODE: RUNTIME_NATIVE})
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Old Core admitted Native")
        assert "custom_components.ha_mcp_tools.native_server" not in sys.modules
        hass.states.async_set("sensor.legacy_core_fixture", "ready")
        secret_path = entry.data[DATA_SECRET_PATH]
        webhook_id = entry.data[DATA_WEBHOOK_ID]
        for url in (
            f"http://127.0.0.1:{manager.port}{secret_path}",
            f"http://127.0.0.1:8123/api/webhook/{webhook_id}",
        ):
            async with llm_api._mcp_session(url) as (session, _):
                tools = await session.list_tools()
                assert "ha_get_state" in {tool.name for tool in tools.tools}
                result = await session.call_tool(
                    "ha_get_state", {"entity_id": "sensor.legacy_core_fixture"}
                )
                wire = result.model_dump(by_alias=True)
                assert not wire["isError"] and "ready" in json.dumps(wire)
        await _assist_read(hass, entry)
        return {
            "phase": phase,
            "core": core_version,
            "conversation_requirements_loaded_by_Core": True,
            "paired_version": paired_version,
            "identity": {
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
                "runtime": entry.options[OPT_RUNTIME_MODE],
                "authentication": entry.options[OPT_WEBHOOK_AUTH],
                "direct_port": manager.port,
                **{
                    key: hashlib.sha256(str(entry.data[key]).encode()).hexdigest()
                    for key in (
                        DATA_ADMIN_TOKEN,
                        DATA_SECRET_PATH,
                        DATA_WEBHOOK_ID,
                        DATA_DCR_SIGNING_KEY,
                        DATA_OAUTH_CLIENT_ID,
                        DATA_OAUTH_CLIENT_SECRET,
                        DATA_OAUTH_SIGNING_KEY,
                    )
                    if key in entry.data
                },
            },
            "contracts": [
                "real_config_flow_Legacy_credentials_and_entry",
                "real_embedded_worker_and_private_SDK",
                "direct_port_and_HA_webhook_state_read",
                "registered_Assist_API_dictionary_result",
                "Native_refused_before_owner_import",
            ],
        }
    finally:
        await embedded_setup.async_teardown_server(hass)
        await hass.async_stop(force=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--phase", choices=("first_start", "full_restart"), required=True
    )
    args = parser.parse_args()
    os.environ["HA_MCP_CONFIG_DIR"] = str(args.config / ".ha_mcp")
    os.environ["HA_MCP_DISABLE_SETTINGS_UI"] = "true"
    sys.path.insert(0, str(args.config))
    diagnostics = CoreDiagnostics()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger().addHandler(diagnostics)
    guard = RejectSharedSDK()
    sys.meta_path.insert(0, guard)
    try:
        proof = asyncio.run(exercise(args.config, args.phase))
        if diagnostics.failures:
            raise RuntimeError("Component emitted blocking/deprecation diagnostics")
        proof["attributed_diagnostics"] = diagnostics.failures
        save(args.output, proof)
    finally:
        sys.meta_path.remove(guard)
        logging.getLogger().removeHandler(diagnostics)


if __name__ == "__main__":
    main()
