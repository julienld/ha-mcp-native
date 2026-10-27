"""Registered WS reads through the real reference consumer and validator.

HA state/services, decorators and transport are simulated explicitly. Registration,
Voluptuous schemas, component readers and server consumers are production code;
no Home Assistant instance, socket, listener or real credentials are used.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.errors import HomeAssistantCommandError, HomeAssistantConnectionError
from ha_mcp.tools import component_config_reads, reference_validator
from ha_mcp.tools.component_api import ComponentCaps

from .test_component_ws_search import (
    _REAL_VOL,
    FakeHass,
    FakeServices,
    FakeState,
    FakeStates,
    _exceptions_stub,
    _FakeConnection,
    _FakeWSApi,
    _StubHomeAssistantError,
    wsapi,
)

_CONFIG = {
    "actions": [
        {
            "action": "light.turn_on",
            "target": {"entity_id": ["light.available", "light.missing"]},
        }
    ]
}


@pytest.fixture
def reference_chain(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Connect the registered handler to the consumer at the simulated HA edge."""
    hass = FakeHass(
        states=[FakeState("light.available")],
        services=FakeServices({"light": {"turn_on": {}}}),
    )
    fake_ws = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake_ws)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", _exceptions_stub)
    wsapi.async_register_commands(hass)
    handler = fake_ws.registered[wsapi.WS_REFERENCE_DATA]
    connection = _FakeConnection()

    def invoke(**kwargs: Any) -> dict[str, Any]:
        params = _REAL_VOL.Schema(handler._ws_schema)(
            {"type": wsapi.WS_REFERENCE_DATA, **kwargs}
        )
        handler(hass, connection, {**params, "id": 1})
        return connection.results[1]

    async def send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == wsapi.WS_REFERENCE_DATA
        try:
            result = await asyncio.to_thread(invoke, **kwargs)
        except (RuntimeError, _StubHomeAssistantError) as exc:
            # Simulate HA's failed command crossing the WS client boundary.
            raise HomeAssistantCommandError(str(exc)) from exc
        return {"success": True, "result": result}

    transport = SimpleNamespace(send_command=AsyncMock(side_effect=send))
    monkeypatch.setattr(
        component_config_reads,
        "get_component_caps",
        AsyncMock(
            return_value=ComponentCaps(1, "2.2.3", frozenset({"reference_data"}), {})
        ),
    )
    monkeypatch.setattr(
        component_config_reads,
        "get_websocket_client",
        AsyncMock(return_value=transport),
    )
    client = SimpleNamespace(
        base_url="http://offline.invalid",
        token="fake",
        get_states=AsyncMock(return_value=[{"entity_id": "light.available"}]),
        get_services=AsyncMock(
            return_value=[{"domain": "light", "services": {"turn_on": {}}}]
        ),
    )
    return SimpleNamespace(
        hass=hass,
        invoke=invoke,
        connection=connection,
        client=client,
        transport=transport,
    )


def test_failed_enumeration_does_not_send_an_authoritative_empty_inventory(
    reference_chain: SimpleNamespace,
) -> None:
    failure = RuntimeError("state enumeration unavailable")
    reference_chain.hass.states.async_all = Mock(side_effect=failure)
    assert reference_chain.hass.states.get("light.available") is not None

    with pytest.raises(RuntimeError) as caught:
        reference_chain.invoke()

    assert caught.value is failure
    assert not reference_chain.connection.results
    reference_chain.hass.states.async_all.assert_called_once_with()


async def test_truly_empty_inventory_is_authoritative_without_rest_fallback(
    reference_chain: SimpleNamespace,
) -> None:
    reference_chain.hass.states = FakeStates([])
    reference_chain.hass.states.async_all = Mock(
        wraps=reference_chain.hass.states.async_all
    )
    assert reference_chain.hass.states.get("light.available") is None
    result = await reference_validator.validate_config_references(
        reference_chain.client, _CONFIG
    )

    assert {(w["kind"], w["value"]) for w in result["warnings"]} == {
        ("entity", "light.available"),
        ("entity", "light.missing"),
    }
    assert reference_chain.connection.results[1]["entity_ids"] == []
    reference_chain.hass.states.async_all.assert_called_once_with()
    reference_chain.client.get_states.assert_not_awaited()
    reference_chain.client.get_services.assert_not_awaited()


async def test_services_only_read_never_enumerates_unavailable_states(
    reference_chain: SimpleNamespace,
) -> None:
    reference_chain.hass.states.async_all = Mock(
        side_effect=RuntimeError("state enumeration unavailable")
    )
    result = await component_config_reads.fetch_reference_data_via_component(
        reference_chain.client, include_states=False
    )

    assert result == {
        "services": [{"domain": "light", "services": {"turn_on": {}}}],
        "entity_ids": [],
    }
    reference_chain.hass.states.async_all.assert_not_called()
    reference_chain.transport.send_command.assert_awaited_once_with(
        wsapi.WS_REFERENCE_DATA, include_states=False
    )


@pytest.mark.parametrize("unavailable", ["states", "services", "invalid_services"])
async def test_inventory_failure_uses_rest_and_warns_only_for_real_missing_references(
    reference_chain: SimpleNamespace, unavailable: str
) -> None:
    if unavailable == "states":
        reference_chain.hass.states.async_all = Mock(
            side_effect=RuntimeError("state enumeration unavailable")
        )
    else:
        reference_chain.hass.services.async_services = Mock(
            side_effect=RuntimeError("service registry unavailable")
            if unavailable == "services"
            else None,
            return_value=None,
        )
    assert reference_chain.hass.states.get("light.available") is not None

    result = await reference_validator.validate_config_references(
        reference_chain.client, _CONFIG
    )

    assert [(w["kind"], w["value"]) for w in result["warnings"]] == [
        ("entity", "light.missing")
    ]
    reference_chain.client.get_states.assert_awaited_once_with()
    reference_chain.client.get_services.assert_awaited_once_with()
    assert not reference_chain.connection.results


@pytest.mark.parametrize("failed_read", ["get_states", "get_services"])
async def test_failed_rest_fallback_reports_skipped_validation_without_false_missing(
    reference_chain: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    failed_read: str,
) -> None:
    reference_chain.hass.states.async_all = Mock(
        side_effect=RuntimeError("state enumeration unavailable")
    )
    getattr(
        reference_chain.client, failed_read
    ).side_effect = HomeAssistantConnectionError("REST inventory unavailable")

    with caplog.at_level(logging.WARNING):
        result = await reference_validator.validate_config_references(
            reference_chain.client, _CONFIG
        )

    assert result == {
        "warnings": [],
        "unvalidated_templates": 0,
        "blueprint_skipped": False,
    }
    reference_chain.client.get_states.assert_awaited_once_with()
    reference_chain.client.get_services.assert_awaited_once_with()
    assert not reference_chain.connection.results
    assert any(
        record.name == reference_validator.__name__
        and "skipping validation for this call" in record.message
        and record.exc_info
        and isinstance(record.exc_info[1], HomeAssistantConnectionError)
        for record in caplog.records
    )
