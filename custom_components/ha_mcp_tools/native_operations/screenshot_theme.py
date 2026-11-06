"""Observe the identified engine user's theme through Core, without a connection."""

from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.tool_api import CallContext
from ha_mcp.utils.config_hash import json_values_equal

from .frontend_user_data import cached_theme_store, check_theme_store
from .registry_access import Authorize, authorize_actor


@dataclass(slots=True)
class NativeThemeObserver:
    hass: Any
    actor: CallContext
    authorize: Authorize = field(repr=False)
    token: str | None = field(default=None, repr=False)
    warnings: list[str] = field(default_factory=list)
    user_id: str | None = field(default=None, init=False)
    before: Any = field(default=None, init=False, repr=False)
    taken: bool = field(default=False, init=False)
    settle_seconds: float = 1.0

    async def _read(self) -> Any:
        await authorize_actor(self.actor, self.authorize)
        refresh = self.hass.auth.async_validate_access_token(self.token)
        if inspect.isawaitable(refresh):
            refresh = await refresh
        if refresh is None or not refresh.user.is_active:
            raise PermissionError("The engine credential cannot be validated locally")
        if self.user_id is not None and refresh.user.id != self.user_id:
            raise PermissionError("The engine credential owner changed")
        self.user_id = refresh.user.id
        pin = cached_theme_store(self.hass, self.user_id)
        await authorize_actor(self.actor, self.authorize)
        check_theme_store(self.hass, pin)
        return copy.deepcopy(pin.store.data.get("theme"))

    async def take_snapshot(self) -> None:
        await authorize_actor(self.actor, self.authorize)
        if self.token is None:
            self.warnings.append(
                "The screenshot engine user's credential is unavailable; its saved theme cannot be observed. The caller's Home Assistant credential was not substituted."
            )
            return
        try:
            self.before = await self._read()
            self.taken = True
        except Exception:  # noqa: BLE001 - advisory cannot prevent a requested render
            self.warnings.append(
                "The screenshot engine user's saved theme could not be read through Core before rendering."
            )
        await authorize_actor(self.actor, self.authorize)

    async def detect_change(self) -> None:
        if not self.taken:
            return
        try:
            await asyncio.sleep(self.settle_seconds)
            current = await self._read()
        except Exception:  # noqa: BLE001 - report an unavailable observation, never repair blindly
            self.warnings.append(
                "The screenshot engine user's saved theme could not be checked after rendering."
            )
            return
        if not json_values_equal(current, self.before):
            self.warnings.append(
                "The screenshot engine changed its user's saved frontend theme, which can affect that account's web and mobile sessions. This observation made no preference changes. Restore the theme from that account's Home Assistant profile, or give the engine a dedicated user."
            )
