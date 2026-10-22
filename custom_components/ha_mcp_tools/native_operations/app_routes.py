"""Bounded app routes on Core's real Ingress view and existing HTTP session.

aiohasupervisor deliberately exposes no wildcard ingress API. The installed
Core view owns URL confinement and its shared session; no handler is called.
"""

import inspect
import re
from dataclasses import dataclass, field
from importlib import import_module
from ipaddress import ip_address
from typing import Any
from urllib.parse import unquote, urlsplit

from multidict import CIMultiDict
from yarl import URL

from ha_mcp.runtime.redaction import register_known_secret_values
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.config_hash import json_values_equal

from .app_actions import AppActions


def ingress_view(hass: Any) -> Any:
    owner_type = import_module("homeassistant.components.hassio.ingress").HassIOIngress
    path = "/api/hassio_ingress/{token}/{path}"
    for route in hass.http.app.router.routes():
        if route.method != "GET" or route.resource.canonical != path:
            continue
        try:
            owner = inspect.getclosurevars(route.handler).nonlocals.get("view")
        except TypeError:
            continue
        if (
            owner is not None
            and type(owner) is owner_type
            and owner.name == "api:hassio:ingress"
        ):
            return owner
    raise UnsupportedOperation("Core Supervisor ingress owner")


def relative_path(path: str) -> tuple[str, str]:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ToolFailure(
            "VALIDATION_ERROR",
            "App paths must be relative to the app root and contain no fragment",
            status="not_started",
        )
    decoded = unquote(parsed.path).lstrip("/")
    if ".." in decoded.split("/") or "\\" in decoded:
        raise ToolFailure(
            "VALIDATION_ERROR",
            "App path contains a traversal component",
            status="not_started",
        )
    return decoded, parsed.query


def routing_identity(info: dict[str, Any]) -> dict[str, Any]:
    return {
        key: info.get(key)
        for key in (
            "slug",
            "version",
            "state",
            "ingress",
            "ingress_entry",
            "ip_address",
            "ingress_port",
        )
    }


@dataclass(slots=True)
class AppRoute:
    service: AppActions
    slug: str
    addon: dict[str, Any]
    path: str
    query: str
    port: int | None
    owner: Any = field(default=None, repr=False)
    session: Any = field(default=None, repr=False)
    url: URL | None = field(default=None, repr=False)
    headers: CIMultiDict[str] = field(default_factory=CIMultiDict, repr=False)

    @classmethod
    async def prepare(
        cls,
        service: AppActions,
        slug: str,
        path: str,
        port: int | None,
        headers: dict[str, str] | None,
    ) -> "AppRoute":
        normalized, query = relative_path(path)
        if port is not None and not 1 <= port <= 65535:
            raise ToolFailure(
                "VALIDATION_ERROR",
                "App port must be between 1 and 65535",
                status="not_started",
            )
        await service.admit()
        addon = await service.supervisor.app_info(slug)
        if addon.get("state") != "started":
            raise ToolFailure(
                "SERVICE_CALL_FAILED", "The app is not running", status="not_started"
            )
        route = cls(
            service,
            slug,
            addon,
            normalized,
            query,
            port,
            headers=CIMultiDict(headers or {}),
        )
        if port is not None:
            address = ip_address(addon.get("ip_address", ""))
            if address.is_loopback or address.is_unspecified:
                raise UnsupportedOperation("App container address is unavailable")
            route.session = service.supervisor.raw.websession
            route.url = URL.build(
                scheme="http", host=str(address), port=port, path="/" + normalized
            ).with_query(query)
        else:
            if not addon.get("ingress"):
                raise UnsupportedOperation(
                    "This app does not expose Supervisor ingress; choose its direct port"
                )
            entry = addon.get("ingress_entry")
            if not isinstance(entry, str) or not re.fullmatch(
                r"/api/hassio_ingress/[A-Za-z0-9_-]+/?", entry
            ):
                raise UnsupportedOperation("Supervisor app ingress identity")
            route.owner = ingress_view(service.manager.hass)
            route.session = route.owner._websession
            token = entry.rstrip("/").rsplit("/", 1)[1]
            route.url = route.owner._create_url(token, normalized).with_query(query)
            route.headers["X-Ingress-Path"] = entry
            route.headers["X-Hass-Source"] = "core.ingress"
        return route

    async def open(self) -> None:
        """Create the actor's session only inside the admitted owned operation."""
        if self.owner is not None:
            if not self.service.actor.user_id:
                raise ToolFailure(
                    "AUTH_FORBIDDEN",
                    "Ingress needs an authenticated user",
                    status="not_started",
                )
            CreateSessionOptions = import_module(
                "aiohasupervisor.models.ingress"
            ).CreateSessionOptions
            session = await self.service.supervisor.client.ingress.create_session(
                CreateSessionOptions(user_id=self.service.actor.user_id)
            )
            if not isinstance(session, str) or not session:
                raise RuntimeError("Supervisor returned no ingress session")
            register_known_secret_values({session})
            self.headers["Cookie"] = f"ingress_session={session}"
        current = await self.service.supervisor.app_info(self.slug)
        if not json_values_equal(
            routing_identity(self.addon), routing_identity(current)
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The app route changed before dispatch",
                status="not_started",
            )
        await self.service.admit()
        self.pin()

    def pin(self) -> None:
        self.service.supervisor.pin()
        if self.owner is not None:
            if (
                ingress_view(self.service.manager.hass) is not self.owner
                or self.owner._websession is not self.session
            ):
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "Core ingress owner changed",
                    status="not_started",
                )
        elif self.service.supervisor.raw.websession is not self.session:
            raise ToolFailure(
                "CONFIG_CONFLICT", "Core app session changed", status="not_started"
            )
        if self.session.closed:
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Core app session is closed",
                status="not_started",
            )

    @property
    def debug_url(self) -> str:
        # Neither the ingress identity/session nor caller credentials belong in
        # a tool response, exception, or log (including explicit debug output).
        return f"app://{self.slug}/" + self.path
