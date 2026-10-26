"""Exercise the cache handler registered on the component's shared WS surface."""

import builtins
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from .test_component_ws_search import _FakeConnection, _FakeWSApi, _Unauthorized, wsapi
from .test_native_engine_backend import native as native
from .test_native_hacs import hacs as hacs
from .test_native_marketplace import marketplace as marketplace

bridge = importlib.import_module(
    "custom_components.ha_mcp_tools.websocket_api.hacs_cache"
)


@pytest.fixture
def registered(hacs, monkeypatch):
    fake = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake)
    # Register the entire surface, exactly as both entry setup paths do.
    wsapi.async_register_commands(hacs.hass)
    return fake.registered["ha_mcp_tools/hacs_cache"]


@pytest.mark.parametrize("provider", ["hacs", "marketplace"])
@pytest.mark.parametrize("topology", ["tools", "server"])
def test_shared_cache_handler_reads_without_runtime_or_tools_services(
    request, registered, provider, topology
):
    store = request.getfixturevalue(provider)
    store.hass.data["ha_mcp_tools"] = {"entry_type": topology}
    store.hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(
            return_value=SimpleNamespace(
                is_active=True, is_admin=True, system_generated=False
            )
        )
    )
    connection = _FakeConnection(user_id="human")
    connection.send_error = Mock()
    registered(
        store.hass,
        connection,
        {"id": 1, "type": "ha_mcp_tools/hacs_cache", "repository_id": "1"},
    )
    connection.send_error.assert_not_called()
    assert connection.results[1]["store"] == provider
    assert connection.results[1]["repository"]["full_name"] == "example/fixture"
    assert connection.results[1]["cache"]["refreshed_from_github"] is False
    assert store.repo.data.new
    store.repo.update_repository.assert_not_awaited()
    store.owner.data.async_write.assert_not_awaited()


def test_cache_handler_requires_admin_before_reading_owner(hacs, registered):
    connection = _FakeConnection(is_admin=False, user_id="viewer")
    with pytest.raises(_Unauthorized):
        registered(hacs.hass, connection, {"id": 1, "repository_id": "1"})


def test_registration_without_a_store_never_imports_optional_owners(hacs, monkeypatch):
    hacs.hass.config.components = {"ha_mcp_tools"}
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith(
            ("custom_components.hacs", "homeassistant.components.marketplace")
        ):
            raise AssertionError("Startup imported an absent store")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    fake = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake)
    wsapi.async_register_commands(hacs.hass)
    assert "ha_mcp_tools/hacs_cache" in fake.registered
    assert "hacs_cache" in wsapi.CAPABILITIES
    hacs.hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(
            return_value=SimpleNamespace(
                is_active=True, is_admin=True, system_generated=False
            )
        )
    )
    connection = _FakeConnection(user_id="human")
    connection.send_error = Mock()
    fake.registered["ha_mcp_tools/hacs_cache"](
        hacs.hass, connection, {"id": 1, "repository_id": "1"}
    )
    assert connection.send_error.call_args.args[1] == "COMPONENT_NOT_INSTALLED"


def test_cache_handler_rechecks_revoked_user_after_projection(hacs, registered):
    active = SimpleNamespace(is_active=True, is_admin=True, system_generated=False)
    revoked = SimpleNamespace(is_active=False, is_admin=True, system_generated=False)
    hacs.hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(side_effect=[active, revoked])
    )
    connection = _FakeConnection(user_id="human")
    connection.send_error = Mock()
    registered(hacs.hass, connection, {"id": 1, "repository_id": "1"})
    assert not connection.results
    assert connection.send_error.call_args.args[1] == "AUTH_FORBIDDEN"
