"""Exchange translated OAuth forms through the installed Core auth owners.

Core 2026.10.2's TokenView already separates grant handling from request-body
parsing. Keep its actual code store and validators, and pass the incoming
Request unchanged so credential providers and failed-login handling see the
client's real address. No Core HTTP handler or local HTTP client is invoked.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from multidict import MultiDict, MultiDictProxy

from .const import DATA_WEBHOOK, DOMAIN, WEBHOOK_AUTH_HA

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class NativeOAuthUnavailable(Exception):
    """No grant has been dispatched; the installed owner is unavailable."""


def _require_active(hass: HomeAssistant, cfg: dict[str, Any]) -> None:
    manager = cfg.get("native_manager")
    if (
        manager is None
        or not manager.is_running
        or hass.data.get(DOMAIN, {}).get(DATA_WEBHOOK) is not cfg
        or cfg.get("auth_mode") != WEBHOOK_AUTH_HA
    ):
        raise NativeOAuthUnavailable("Native OAuth entry is no longer active")


def _token_owner(hass: HomeAssistant) -> Any:
    from homeassistant.components.auth import TokenView

    for route in hass.http.app.router.routes():
        if route.method != "POST" or route.resource.canonical != "/auth/token":
            continue
        try:
            owner = inspect.getclosurevars(route.handler).nonlocals.get("view")
        except TypeError:
            continue
        if (
            isinstance(owner, TokenView)
            and owner.name == "api:auth:token"
            and owner.url == "/auth/token"
        ):
            return owner
    raise NativeOAuthUnavailable("Core token owner is not available")


async def _grant(
    owner: Any, request: web.Request, hass: HomeAssistant, form: MultiDictProxy[str]
) -> web.Response:
    grant_type = form.get("grant_type")
    if grant_type == "authorization_code":
        return cast(
            web.Response, await owner._async_handle_auth_code(hass, form, request)
        )
    if grant_type == "refresh_token":
        return cast(
            web.Response, await owner._async_handle_refresh_token(hass, form, request)
        )
    return web.json_response({"error": "unsupported_grant_type"}, status=400)


async def async_forward_core(
    hass: HomeAssistant,
    cfg: dict[str, Any],
    path: str,
    form: MultiDict[str],
    request: web.Request,
) -> tuple[int, bytes, str]:
    """Use Core's live grant owner, or its token registry for RFC 7009 revoke."""
    _require_active(hass, cfg)
    if path not in {"/auth/token", "/auth/revoke"}:
        raise NativeOAuthUnavailable("Unsupported native OAuth operation")
    if path == "/auth/revoke" or form.get("action") == "revoke":
        if (token := form.get("token")) is not None and (
            refresh := hass.auth.async_get_refresh_token_by_token(token)
        ) is not None:
            hass.auth.async_remove_refresh_token(refresh)
        return 200, b"", "application/octet-stream"

    from homeassistant.components.http.ban import log_invalid_auth

    owner = _token_owner(hass)
    if not all(
        callable(getattr(owner, name, None))
        for name in ("_async_handle_auth_code", "_async_handle_refresh_token")
    ):
        raise NativeOAuthUnavailable("Core token owner contract is unavailable")
    # Like TokenView.post, this dispatch applies Core's failed-login decorator.
    # The original request and the translated form remain separate arguments.
    response = await log_invalid_auth(_grant)(
        owner, request, hass, MultiDictProxy(form)
    )
    if not isinstance(response.body, bytes):
        raise RuntimeError("Core token owner returned an unsupported response body")
    return response.status, response.body, response.content_type
