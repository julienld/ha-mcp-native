"""Manage Frontend defaults and the proven screenshot engine user's profile."""

import asyncio
import copy
import inspect
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import Context

from ha_mcp.errors import create_validation_error
from ha_mcp.runtime.health_results import summarize_theme_listing
from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.runtime.theme_values import backend_theme_data, engine_theme_value
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ..helper_collections import _INVALID_ERRORS
from .command_validation import _validated_payload
from .frontend_themes import theme_image
from .frontend_user_data import ThemeStorePin, cached_theme_store, check_theme_store
from .registry_access import admit_registry_write, authorize_actor, registry_json


@dataclass(slots=True)
class NativeThemes:
    manager: Any
    actor: CallContext
    started: bool = field(default=False, init=False)
    completed: bool = field(default=False, init=False)

    @property
    def hass(self) -> Any:
        return self.manager.hass

    async def _authorize(self) -> None:
        await authorize_actor(self.actor, self.manager.backend.authorize)

    async def _admit(self) -> None:
        await admit_registry_write(self.actor, self.manager.backend.authorize)

    def _listing(self) -> dict[str, Any]:
        return summarize_theme_listing(theme_image(self.hass))

    def _theme_service(self) -> Any:
        service = self.hass.services.async_services_for_domain("frontend").get(
            "set_theme"
        )
        if service is None or not callable(service.schema):
            raise UnsupportedOperation("frontend.set_theme")
        return service

    @staticmethod
    def _validated_service(service: Any, data: dict[str, Any]) -> dict[str, Any]:
        try:
            validated: dict[str, Any] = service.schema(copy.deepcopy(data))
            return validated
        except _INVALID_ERRORS as error:
            payload = create_validation_error(str(error))
            raise ToolFailure(
                payload["error"]["code"],
                payload["error"]["message"],
                data=payload,
                status="not_started",
            ) from error

    async def _set_default(self, name: str | None, mode: str | None) -> dict[str, Any]:
        data = backend_theme_data(name, mode)
        async with registry_update_lock("frontend.theme", "backend"):
            service = self._theme_service()
            self._validated_service(service, data)
            await await_owned_io(self._dispatch_default(service, data))
            await self._authorize()
            listing = self._listing()
            from homeassistant.components.frontend import DEFAULT_THEME, VALUE_NO_THEME

            expected = DEFAULT_THEME if mode != "dark" else None
            if name != VALUE_NO_THEME:
                expected = name
            actual = listing[
                "default_dark_theme" if mode == "dark" else "default_theme"
            ]
            if not json_values_equal(actual, expected):
                raise RuntimeError(
                    "Core default theme changed during write verification"
                )
            return {
                "success": True,
                "data": {
                    "theme": name,
                    "mode": mode or "light",
                    "default_theme": listing.get("default_theme"),
                    "default_dark_theme": listing.get("default_dark_theme"),
                },
            }

    async def _dispatch_default(self, service: Any, data: dict[str, Any]) -> None:
        await self._admit()
        if self._theme_service() is not service:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The Core theme service changed before dispatch.",
                status="not_started",
            )
        self._validated_service(service, data)
        check_mutation_admission(self.actor)
        self.started = True
        await self.hass.services.async_call(
            "frontend",
            "set_theme",
            copy.deepcopy(data),
            blocking=True,
            context=Context(user_id=self.actor.user_id),
        )
        self.completed = True

    async def _engine_token(self) -> str:
        from ..native_screenshots import NativeScreenshots

        await self._authorize()
        _url, token = await NativeScreenshots(self.manager, self.actor)._target()
        await self._authorize()
        if token is None:
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "The screenshot engine's Home Assistant account cannot be identified. Use that account's own Home Assistant profile to change its theme.",
                status="not_started",
            )
        return token

    def _engine_principal(self, token: str, expected: str | None = None) -> str:
        refresh = self.hass.auth.async_validate_access_token(token)
        if inspect.isawaitable(refresh):
            if inspect.iscoroutine(refresh):
                refresh.close()
            raise UnsupportedOperation("frontend.engine_theme.synchronous_auth_owner")
        if refresh is None or not refresh.user.is_active:
            raise ToolFailure(
                "AUTH_FORBIDDEN",
                "The engine credential is no longer valid locally.",
                status="not_started",
            )
        identifier: str = refresh.user.id
        if expected is not None and identifier != expected:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The engine credential owner changed.",
                status="not_started",
            )
        return identifier

    async def _engine_user(self, token: str, expected: str | None = None) -> str:
        await self._authorize()
        return self._engine_principal(token, expected)

    async def _engine_store(
        self, token: str, identifier: str, *, initialize: bool = False
    ) -> ThemeStorePin:
        await self._engine_user(token, identifier)
        try:
            return cached_theme_store(self.hass, identifier)
        except ToolFailure as error:
            if not initialize or error.code != "SERVICE_UNAVAILABLE":
                raise
        return await await_owned_io(self._load_engine_store(token, identifier))

    async def _load_engine_store(self, token: str, identifier: str) -> ThemeStorePin:
        from homeassistant.components.frontend.storage import async_user_store

        await self._admit()
        self._engine_principal(token, identifier)
        # Core loading can migrate/save or repair a corrupt store. This path
        # therefore belongs to the admitted write, never to a read operation.
        self.started = True
        store = await async_user_store(self.hass, identifier)
        await self._engine_user(token, identifier)
        pin = cached_theme_store(self.hass, identifier)
        if pin.store is not store:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The Core engine user store changed during loading.",
                status="not_started",
            )
        return pin

    async def _engine(
        self,
        *,
        write: bool,
        value: dict[str, Any] | None,
        expected: dict[str, Any] | None,
        force: bool,
    ) -> dict[str, Any]:
        candidate = registry_json(engine_theme_value(value)) if write else None
        expected = copy.deepcopy(expected)
        values = (
            _validated_payload(
                self.hass,
                "frontend/set_user_data",
                {"key": "theme", "value": candidate},
            )
            if write
            else None
        )
        token = await self._engine_token()
        identifier = await self._engine_user(token)
        async with registry_update_lock("frontend.theme.user", identifier):
            pin = await self._engine_store(token, identifier, initialize=write)
            if not write:
                return {
                    "success": True,
                    "data": {"theme": copy.deepcopy(pin.store.data.get("theme"))},
                }
            assert values is not None
            await await_owned_io(
                self._dispatch_engine(
                    token, identifier, pin, values["value"], expected, force
                )
            )
            await self._engine_user(token, identifier)
            check_theme_store(self.hass, pin)
            current = copy.deepcopy(pin.store.data.get("theme"))
            if not json_values_equal(current, candidate):
                raise RuntimeError(
                    "Core engine theme changed during write verification"
                )
            return {"success": True, "data": {"theme": current}}

    async def _dispatch_engine(
        self,
        token: str,
        identifier: str,
        pin: ThemeStorePin,
        candidate: dict[str, Any],
        expected: dict[str, Any] | None,
        force: bool,
    ) -> None:
        await self._admit()
        current_store = await self._engine_store(token, identifier)
        await self._admit()
        self._engine_principal(token, identifier)
        check_theme_store(self.hass, pin)
        if (
            current_store.future is not pin.future
            or current_store.store is not pin.store
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The Core engine user store changed before dispatch.",
                status="not_started",
            )
        if not force and not json_values_equal(pin.store.data.get("theme"), expected):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The engine account's saved theme changed since the expected value; it was not overwritten.",
                status="not_started",
            )
        check_mutation_admission(self.actor)
        self.started = True
        await pin.store.async_set_item("theme", copy.deepcopy(candidate))
        self.completed = True

    async def manage(
        self,
        action: str,
        theme_name: str | None = None,
        expected_current: dict[str, Any] | None = None,
        force: bool = False,
        value: dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        try:
            await self._authorize()
            if action == "list":
                return {"success": True, "data": self._listing()}
            if action == "set":
                return await self._set_default(theme_name, mode)
            return await self._engine(
                write=action == "set_engine_theme",
                value=value,
                expected=expected_current,
                force=force,
            )
        except asyncio.CancelledError as error:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            raise self._failure() from error
        except Exception as error:
            if self.started:
                raise self._failure() from error
            raise

    def _failure(self) -> ToolFailure:
        if not self.started:
            return ToolFailure(
                "CORE_READ_FAILED",
                "The Core theme owner cancelled before its write.",
                status="not_started",
            )
        return ToolFailure(
            "OUTCOME_UNKNOWN",
            "A Core theme write started; inspect its current value before retrying.",
            data={
                "success": False,
                "error": {
                    "code": "OUTCOME_UNKNOWN",
                    "message": "Theme write outcome could not be verified.",
                },
                "write_completed": self.completed,
            },
            status="outcome_unknown",
        )
