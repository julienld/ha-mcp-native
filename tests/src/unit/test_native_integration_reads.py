"""Integration inspection uses Core owners and discloses flow effects."""

import asyncio
import copy
import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.integration_reads import (
    IntegrationReadTools,
    inspection_starts_flow,
)
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native
from .test_native_engine_flows import flows as flows
from .test_native_engine_full_flows import _setup


def reader(backend: Any) -> Engine:
    return Engine(
        backend,
        [
            define_tool(
                backend,
                IntegrationReadTools,
                "ha_get_integration",
                annotations={"readOnlyHint": False},
                writes=lambda args: inspection_starts_flow(
                    args, redact=backend.redaction_enabled
                ),
            )
        ],
    )


def inspection(native: Any, monkeypatch: pytest.MonkeyPatch) -> tuple:
    hass, user, entry, manager, backend, _ = _setup(native, monkeypatch)
    entry.options = {"threshold": 7, "password": "private-test-value"}
    entry.supports_options = True
    entry.source = "user"
    entry.state = "loaded"
    entry.subentries = {
        "sub": SimpleNamespace(
            subentry_id="sub",
            subentry_type="sensor",
            title="Child",
            unique_id="identity",
        )
    }
    hass.config_entries.async_entries = lambda domain=None: (
        [entry] if domain in {None, entry.domain} else []
    )
    command = importlib.import_module(
        "custom_components.ha_mcp_tools.websocket_api.config_entries"
    )
    monkeypatch.setattr(
        command,
        "_load_secret_scrub",
        lambda _hass: (frozenset({"private-test-value"}), False),
    )
    hass.async_add_executor_job = AsyncMock(
        side_effect=lambda function, *args: function(*args)
    )
    logger = SimpleNamespace(getEffectiveLevel=lambda: 10)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.logger.helpers",
        SimpleNamespace(DATA_LOGGER="logger", get_logger=lambda _name: logger),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(DISCOVERY_SOURCES={"discovery"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.setup",
        SimpleNamespace(async_get_loaded_integrations=lambda _hass: {entry.domain}),
    )
    manager.async_progress = list
    hass.data["logger"] = SimpleNamespace(
        settings=SimpleNamespace(async_get_integration_domains=list)
    )
    return reader(backend), hass, user, entry, manager


async def test_native_details_and_paginated_options_are_real_readonly_values(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, _, entry, manager = inspection(native, monkeypatch)
    try:
        actor = CallContext("actor", read_only=True)
        detail = await engine.call(
            "ha_get_integration",
            {"entry_id": "entry", "include_subentries": True},
            actor,
        )
        assert not detail.is_error, detail.data
        assert detail.data["entry"]["options"] == {
            "threshold": 7,
            "password": "**redacted**",
        }
        assert detail.data["subentries"][0]["subentry_id"] == "sub"
        assert "data" not in detail.data["entry"]
        assert (
            detail.data["log_level"] == "DEBUG" and detail.data["log_level_raw"] == 10
        )
        listing = await engine.call(
            "ha_get_integration",
            {"domain": entry.domain, "query": "Example", "limit": 1},
            actor,
        )
        assert not listing.is_error, listing.data
        assert listing.data["entries"][0]["options"] == detail.data["entry"]["options"]
        missing = await engine.call("ha_get_integration", {"entry_id": "absent"}, actor)
        assert missing.is_error
        manager.async_init.assert_not_awaited()
    finally:
        await engine.backend.stop()


@pytest.mark.parametrize("probe", ["options", "subentry", "redaction"])
@pytest.mark.parametrize("failure", ["callback", "serialization"])
async def test_schema_and_redaction_probes_are_gated_and_unknown_is_preserved(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch, probe: str, failure: str
) -> None:
    engine, _, _, _, manager = inspection(native, monkeypatch)
    engine.backend.redaction_enabled = lambda: probe == "redaction"
    arguments = {"entry_id": "entry"}
    if probe == "options":
        arguments["include_schema"] = True
    elif probe == "subentry":
        arguments.update(include_subentry_schema=True, subentry_type="sensor")
    calls = []

    async def wrote_then_failed(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("written")
        if failure == "serialization":
            return {
                "flow_id": "finished",
                "type": "create_entry",
                "invalid_json": object(),
            }
        raise ValueError("after entry write")

    try:
        with pytest.raises(ToolFailure) as denied:
            await engine.call(
                "ha_get_integration", arguments, CallContext("actor", read_only=True)
            )
        assert denied.value.code == "READ_ONLY"
        manager.async_init.assert_not_awaited()
        manager.async_init.side_effect = wrote_then_failed
        unknown = await engine.call(
            "ha_get_integration", arguments, CallContext("actor")
        )
        assert unknown.is_error and unknown.status == "outcome_unknown", unknown
        assert unknown.data["error"]["code"] == "OUTCOME_UNKNOWN"
        assert calls == ["written"]
    finally:
        await engine.backend.stop()


async def test_native_options_and_subentry_forms_close_owned_flows(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, _, _, manager = inspection(native, monkeypatch)
    manager.async_init.side_effect = [
        {
            "flow_id": "options",
            "type": "form",
            "step_id": "init",
            "data_schema": [{"name": "threshold", "default": 99}],
        },
        {
            "flow_id": "child",
            "type": "form",
            "step_id": "user",
            "data_schema": [{"name": "name"}],
        },
    ]
    try:
        result = await engine.call(
            "ha_get_integration",
            {
                "entry_id": "entry",
                "include_schema": True,
                "include_subentry_schema": True,
                "subentry_type": "sensor",
            },
            CallContext("actor"),
        )
        assert not result.is_error, result.data
        assert result.data["entry"]["options"]["threshold"] == 7
        assert result.data["options_schema"]["data_schema"][0]["default"] == 99
        assert result.data["subentry_schema"]["flow_type"] == "form"
        assert manager.async_abort.call_count == 2
        assert not engine.backend._flows.owned
    finally:
        await engine.backend.stop()


@pytest.mark.parametrize("device_id", [None, "device", "other"])
async def test_native_diagnostics_and_knx_use_owners_and_shared_projection(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch, device_id: str | None
) -> None:
    engine, hass, _, entry, _ = inspection(native, monkeypatch)
    entry.domain = "knx"
    artifact = {
        "home_assistant": {"version": "test"},
        "data": {"items": [{"id": 1}, {"id": 2}]},
    }
    callback = AsyncMock(return_value=artifact["data"])
    device_callback = AsyncMock(return_value=artifact["data"])
    device = SimpleNamespace(
        id="device",
        name="Device",
        config_entry_id="entry" if device_id == "device" else "another-entry",
    )
    formatter = AsyncMock(
        return_value=SimpleNamespace(status=200, text=json.dumps(artifact))
    )
    owner = SimpleNamespace(
        _DIAGNOSTICS_DATA="diagnostics", _async_get_json_file_response=formatter
    )
    components = sys.modules["homeassistant.components"]
    helpers = sys.modules["homeassistant.helpers"]
    monkeypatch.setattr(components, "diagnostics", owner, raising=False)
    monkeypatch.setattr(
        helpers,
        "issue_registry",
        SimpleNamespace(async_get=lambda _hass: SimpleNamespace(issues={})),
        raising=False,
    )
    monkeypatch.setattr(
        helpers,
        "device_registry",
        SimpleNamespace(
            async_get=lambda _hass: SimpleNamespace(
                async_get=lambda *_args, **_kwargs: device
            )
        ),
        raising=False,
    )
    hass.data["diagnostics"] = SimpleNamespace(
        platforms={
            "knx": SimpleNamespace(
                config_entry_diagnostics=callback, device_diagnostics=device_callback
            )
        }
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.knx.const",
        SimpleNamespace(KNX_MODULE_KEY="knx"),
    )
    knx = AsyncMock(
        return_value={
            "group_addresses": {"1/0/1": {"name": "Light"}},
            "info": {"name": "Home"},
        }
    )
    hass.data["knx"] = SimpleNamespace(project=SimpleNamespace(get_knxproject=knx))
    result = await engine.call(
        "ha_get_integration",
        {
            "entry_id": "entry",
            "include_diagnostics": True,
            "diagnostics_data_path": "data.items",
            "diagnostics_data_offset": 1,
            "diagnostics_data_limit": 1,
            "include_knx_project": True,
            "device_id": device_id,
        },
        CallContext("actor", read_only=True),
    )
    assert not result.is_error, result.data
    assert result.data["knx_project"]["count"] == 1
    knx.assert_awaited_once()
    if device_id == "other":
        assert "Diagnostics not available" in result.data["diagnostics"]["error"]
        callback.assert_not_awaited()
        device_callback.assert_not_awaited()
        formatter.assert_not_awaited()
        return
    assert result.data["diagnostics"]["data"]["items"] == [{"id": 2}]
    if device_id:
        device_callback.assert_awaited_once_with(hass, entry, device)
        callback.assert_not_awaited()
        filename = "knx-entry-Device-device"
    else:
        callback.assert_awaited_once_with(hass, entry)
        device_callback.assert_not_awaited()
        filename = "config_entry-knx-entry"
    formatter.assert_awaited_once_with(
        hass, artifact["data"], [], filename, "knx", "entry", device_id
    )


async def test_remote_getter_runs_same_body_with_existing_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.backends.remote import RemoteBackend
    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.tools import tools_integrations

    rows = [
        {
            "entry_id": "entry",
            "domain": "demo",
            "title": "Demo",
            "state": "loaded",
            "options": {"nested": {"threshold": 7}},
            "subentries": [],
        }
    ]
    fetch = AsyncMock(return_value=copy.deepcopy(rows))
    monkeypatch.setattr(tools_integrations, "_fetch_entries_via_component", fetch)
    client = HomeAssistantClient("http://unused.example", "test-only")
    monkeypatch.setattr(
        client,
        "send_websocket_message",
        AsyncMock(return_value={"success": True, "result": []}),
    )
    backend = RemoteBackend(client)
    engine = Engine(
        backend,
        [
            define_tool(
                backend,
                IntegrationReadTools,
                "ha_get_integration",
                annotations={"readOnlyHint": False},
            )
        ],
    )
    try:
        result = await engine.call(
            "ha_get_integration", {"entry_id": "entry"}, CallContext(None)
        )
        assert not result.is_error, result.data
        assert result.data["entry"]["options"] == rows[0]["options"]
        fetch.assert_awaited_once_with(client, entry_id="entry")
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "logger/log_info"}
        )
    finally:
        await client.close()


async def test_uncertain_bulk_probe_drains_other_owned_probes_before_return(
    native: Any, flows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, hass, _, entry, manager = inspection(native, monkeypatch)
    engine.backend.redaction_enabled = lambda: True
    second = copy.copy(entry)
    second.entry_id = "second"
    hass.config_entries.async_entries = lambda domain=None: [entry, second]
    started, failed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def probe(handler: str, **kwargs: Any) -> dict:
        if handler == "entry":
            await started.wait()
            failed.set()
            raise ValueError("first probe committed then failed")
        started.set()
        await release.wait()
        return {"flow_id": "second-flow", "type": "form", "data_schema": []}

    manager.async_init.side_effect = probe
    task = asyncio.create_task(
        engine.call(
            "ha_get_integration", {"include_options": True}, CallContext("actor")
        )
    )
    try:
        await asyncio.wait_for(failed.wait(), 2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
    finally:
        release.set()
        result = await asyncio.wait_for(task, 2)
        await engine.backend.stop()
    assert result.is_error and result.status == "outcome_unknown"
    assert not engine.backend._flows.owned
    assert manager.async_init.await_count == 2
    manager.async_abort.assert_called_once_with("second-flow")
