"""Theme orchestration pins actual Frontend and engine-account owner seams."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from .test_component_ws_search import _REAL_VOL as vol
from .test_native_engine_backend import native as native


class UserStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {"theme": {"theme": "blue", "dark": False}}
        self.calls = []
        self.fail = False
        self.cancel = False
        self.alter = False
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def async_set_item(self, key: str, value: Any) -> None:
        self.calls.append((key, value))
        self.data[key] = value
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.alter:
            value["dark"] = 0
        if self.fail:
            raise RuntimeError("Store failed after applying the preference")
        if self.cancel:
            raise asyncio.CancelledError("Core store cancelled after its effect")


@pytest.fixture
async def themes(native: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    frontend = SimpleNamespace(
        DATA_THEMES="themes",
        DATA_DEFAULT_THEME="default_theme",
        DATA_DEFAULT_DARK_THEME="default_dark_theme",
        DEFAULT_THEME="default",
        VALUE_NO_THEME="none",
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.frontend", frontend)
    store = UserStore()
    future = asyncio.get_running_loop().create_future()
    future.set_result(store)
    user = SimpleNamespace(id="engine-user", is_active=True)
    tokens = {"engine-token": SimpleNamespace(user=user)}
    hass = SimpleNamespace(
        config=SimpleNamespace(safe_mode=False, recovery_mode=False),
        data={
            "themes": {"blue": {"primary-color": "blue"}, "night": {}},
            "default_theme": "blue",
            "default_dark_theme": None,
            "frontend_storage": {"engine-user": future},
        },
        auth=SimpleNamespace(async_validate_access_token=tokens.get),
    )
    registration = SimpleNamespace(
        schema=vol.Schema(
            {
                vol.Required("name"): vol.In(["blue", "night", "default", "none"]),
                vol.Optional("mode"): vol.In(["light", "dark"]),
            }
        )
    )
    world = SimpleNamespace(
        hass=hass,
        tokens=tokens,
        user=user,
        store=store,
        registration=registration,
        service_calls=[],
        fail_service=False,
        cancel_service=False,
        on_load=None,
        load_calls=[],
    )

    async def service(
        domain: str, action: str, data: dict[str, Any], **kwargs: Any
    ) -> None:
        assert (domain, action) == ("frontend", "set_theme")
        world.service_calls.append((data, kwargs))
        key = "default_dark_theme" if data.get("mode") == "dark" else "default_theme"
        hass.data[key] = (
            (None if key == "default_dark_theme" else "default")
            if data["name"] == "none"
            else data["name"]
        )
        if world.fail_service:
            raise RuntimeError("Service failed after applying the default")
        if world.cancel_service:
            raise asyncio.CancelledError("Core service cancelled after its effect")

    hass.services = SimpleNamespace(
        async_services_for_domain=lambda domain: {"set_theme": registration},
        async_call=service,
    )

    async def load(_hass: Any, user_id: str) -> Any:
        assert user_id == "engine-user"
        world.load_calls.append(user_id)
        if user_id not in hass.data["frontend_storage"]:
            loaded = asyncio.get_running_loop().create_future()
            loaded.set_result(store)
            hass.data["frontend_storage"][user_id] = loaded
        if world.on_load:
            world.on_load()
        return store

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.frontend.storage",
        SimpleNamespace(
            UserStore=UserStore, DATA_STORAGE="frontend_storage", async_user_store=load
        ),
    )
    hass.data["websocket_api"] = {
        "frontend/set_user_data": (
            object(),
            vol.Schema({"id": int, "type": str, "key": str, "value": dict}),
        )
    }
    screenshots = importlib.import_module(
        "custom_components.ha_mcp_tools.native_screenshots"
    )
    monkeypatch.setattr(
        screenshots.NativeScreenshots,
        "_target",
        AsyncMock(return_value=("http://engine.invalid", "engine-token")),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.themes"
    )
    monkeypatch.setattr(module, "_INVALID_ERRORS", (vol.Invalid, ValueError))
    registry_access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(registry_access, "json_bytes", native.json_bytes)
    monkeypatch.setattr(module, "Context", SimpleNamespace)
    bindings = importlib.import_module(
        "custom_components.ha_mcp_tools.native_theme_tools"
    )
    manager = SimpleNamespace(hass=hass, backend=SimpleNamespace(authorize=AsyncMock()))
    world.manager, world.module, world.target = (
        manager,
        module,
        screenshots.NativeScreenshots._target,
    )
    world.engine = Engine(manager.backend, [bindings.theme_definition(manager)])
    return world


async def test_native_theme_reads_library_and_the_engine_profile_without_writing(
    themes: Any,
) -> None:
    listed = await themes.engine.call(
        "ha_manage_theme", {"action": "list"}, CallContext("actor", read_only=True)
    )
    assert not listed.is_error and listed.data["data"]["themes"] == ["blue", "night"]
    profile = await themes.engine.call(
        "ha_manage_theme",
        {"action": "get_engine_theme"},
        CallContext("actor", read_only=True),
    )
    assert not profile.is_error and profile.data["data"]["theme"] == {
        "theme": "blue",
        "dark": False,
    }
    assert not themes.store.calls and not themes.service_calls


async def test_native_theme_set_uses_core_validation_actor_and_effective_dark_default(
    themes: Any,
) -> None:
    set_theme = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set", "theme_name": "night", "mode": "dark"},
        CallContext("actor"),
    )
    assert (
        not set_theme.is_error
        and set_theme.data["data"]["default_dark_theme"] == "night"
    )
    data, kwargs = themes.service_calls[0]
    assert data == {"name": "night", "mode": "dark"} and kwargs["blocking"] is True
    assert kwargs["context"].user_id == "actor"
    reset = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set", "theme_name": "none", "mode": "dark"},
        CallContext("actor"),
    )
    assert not reset.is_error and reset.data["data"]["default_dark_theme"] is None
    invalid = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set", "theme_name": "missing"},
        CallContext("actor"),
    )
    assert invalid.is_error and invalid.status == "not_started"
    assert len(themes.service_calls) == 2


@pytest.mark.parametrize("action", ["set", "set_engine_theme"])
async def test_native_theme_write_is_rejected_in_read_only_before_discovery_or_service(
    themes: Any, action: str
) -> None:
    with pytest.raises(ToolFailure) as failure:
        await themes.engine.call(
            "ha_manage_theme",
            {"action": action, "theme_name": "night", "value": {}},
            CallContext("actor", read_only=True),
        )
    assert failure.value.code == "READ_ONLY"
    themes.target.assert_not_awaited()
    assert not themes.store.calls and not themes.service_calls


async def test_native_engine_theme_never_substitutes_the_actor_when_engine_token_is_unknown(
    themes: Any,
) -> None:
    themes.target.return_value = ("http://external.invalid", None)
    result = await themes.engine.call(
        "ha_manage_theme", {"action": "get_engine_theme"}, CallContext("actor")
    )
    assert result.is_error and result.status == "not_started"
    assert "cannot be identified" in str(result.data)
    assert not themes.store.calls


@pytest.mark.parametrize("cold", ["absent", "pending"])
async def test_native_engine_theme_read_does_not_initialize_or_cancel_a_core_store(
    themes: Any, cold: str
) -> None:
    stores = themes.hass.data["frontend_storage"]
    stores.clear()
    pending = asyncio.get_running_loop().create_future()
    if cold == "pending":
        stores["engine-user"] = pending
    result = await themes.engine.call(
        "ha_manage_theme",
        {"action": "get_engine_theme"},
        CallContext("actor", read_only=True),
    )
    assert result.is_error and result.status == "not_started"
    assert themes.load_calls == [] and not pending.cancelled()


async def test_native_engine_theme_checks_the_live_validator_before_initializing_storage(
    themes: Any,
) -> None:
    themes.hass.data["frontend_storage"].clear()
    themes.hass.data["websocket_api"].clear()
    result = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set_engine_theme", "value": {}, "force": True},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started"
    assert themes.load_calls == [] and not themes.store.calls


async def test_native_engine_theme_guard_distinguishes_boolean_and_number(
    themes: Any,
) -> None:
    themes.store.data["theme"] = {"dark": 0}
    result = await themes.engine.call(
        "ha_manage_theme",
        {
            "action": "set_engine_theme",
            "value": {},
            "expected_current": {"dark": False},
        },
        CallContext("actor"),
    )
    assert result.is_error and result.status == "not_started" and not themes.store.calls
    forced = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set_engine_theme", "value": {}, "force": True},
        CallContext("actor"),
    )
    assert not forced.is_error and themes.store.data["theme"] == {}


@pytest.mark.parametrize("change", ["token", "store"])
async def test_native_engine_theme_revalidates_credential_and_owner_after_loading(
    themes: Any, change: str
) -> None:
    themes.hass.data["frontend_storage"].clear()

    def alter() -> None:
        if change == "token":
            themes.tokens.clear()
        else:
            future = asyncio.get_running_loop().create_future()
            future.set_result(UserStore())
            themes.hass.data["frontend_storage"]["engine-user"] = future

    themes.on_load = alter
    result = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set_engine_theme", "value": {}, "force": True},
        CallContext("actor"),
    )
    # Loading can migrate storage before the credential/owner changes. No
    # preference write is dispatched, but the overall effect is not rejected.
    assert (
        result.is_error
        and result.status == "outcome_unknown"
        and not themes.store.calls
    )


async def test_native_theme_default_write_rechecks_actor_after_schema_preflight(
    themes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = themes.module.NativeThemes._validated_service

    def validate(service: Any, data: dict[str, Any]) -> Any:
        result = original(service, data)
        themes.manager.backend.authorize.side_effect = PermissionError("revoked")
        return result

    monkeypatch.setattr(
        themes.module.NativeThemes, "_validated_service", staticmethod(validate)
    )
    result = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set", "theme_name": "night"},
        CallContext("actor"),
    )
    assert (
        result.is_error and result.status == "not_started" and not themes.service_calls
    )


async def test_native_engine_theme_rechecks_token_after_final_actor_authorization(
    themes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = themes.module.NativeThemes._engine_store

    async def load(owner: Any, token: str, identifier: str) -> Any:
        store = await original(owner, token, identifier)

        async def authorize(_actor: Any) -> None:
            themes.tokens.clear()

        themes.manager.backend.authorize.side_effect = authorize
        return store

    monkeypatch.setattr(themes.module.NativeThemes, "_engine_store", load)
    owner = themes.module.NativeThemes(themes.manager, CallContext("actor"))
    with pytest.raises(ToolFailure) as error:
        await owner._dispatch_engine(
            "engine-token", "engine-user", themes.store, {}, None, True
        )
    assert error.value.code == "AUTH_FORBIDDEN" and not themes.store.calls


@pytest.mark.parametrize("action", ["set", "set_engine_theme"])
async def test_native_theme_error_after_effect_is_unknown_and_is_not_replayed(
    themes: Any, action: str
) -> None:
    themes.fail_service = themes.store.fail = True
    result = await themes.engine.call(
        "ha_manage_theme",
        {"action": action, "theme_name": "night", "value": {}, "force": True},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    assert len(themes.service_calls if action == "set" else themes.store.calls) == 1


@pytest.mark.parametrize("action", ["set", "set_engine_theme"])
async def test_native_theme_core_cancellation_after_effect_returns_unknown(
    themes: Any, action: str
) -> None:
    themes.cancel_service = themes.store.cancel = True
    result = await themes.engine.call(
        "ha_manage_theme",
        {"action": action, "theme_name": "night", "value": {}, "force": True},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert len(themes.service_calls if action == "set" else themes.store.calls) == 1


async def test_native_engine_theme_listener_cannot_alter_the_detached_expected_payload(
    themes: Any,
) -> None:
    themes.store.alter = True
    value = {"theme": "night", "dark": False}
    result = await themes.engine.call(
        "ha_manage_theme",
        {"action": "set_engine_theme", "value": value, "force": True},
        CallContext("actor"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert value["dark"] is False and themes.store.data["theme"]["dark"] == 0


async def test_native_engine_theme_drains_a_cancelled_write_before_releasing_its_lock(
    themes: Any,
) -> None:
    themes.store.block = True
    first = asyncio.create_task(
        themes.module.NativeThemes(themes.manager, CallContext("actor")).manage(
            "set_engine_theme", value={"theme": "night"}, force=True
        )
    )
    await themes.store.entered.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(
        themes.module.NativeThemes(themes.manager, CallContext("actor")).manage(
            "set_engine_theme", value={}, expected_current={"theme": "night"}
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not first.done() and not second.done() and len(themes.store.calls) == 1
    themes.store.release.set()
    with pytest.raises(asyncio.CancelledError):
        _ = await first
    result = await second
    assert (
        result["success"]
        and themes.store.data["theme"] == {}
        and len(themes.store.calls) == 2
    )
