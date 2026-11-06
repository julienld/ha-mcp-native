"""Borrow Core's existing Supervisor owners; only named app reads are exposed."""

from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.app_values import _validate_supervisor_slug
from ha_mcp.tool_api import CallContext, ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from ..optional_integrations import loaded_integration_module
from .registry_access import authorize_actor


@dataclass(slots=True)
class SupervisorAccess:
    manager: Any
    actor: CallContext
    raw: Any = field(default=None, init=False, repr=False)
    client: Any = field(default=None, init=False, repr=False)

    def _owners(self) -> tuple[Any, Any]:
        hass = self.manager.hass
        if "hassio" not in hass.config.components:
            raise UnsupportedOperation(
                "Supervisor is unavailable on this Home Assistant installation"
            )
        get_supervisor_client = loaded_integration_module(
            hass, "hassio"
        ).get_supervisor_client
        DATA_COMPONENT = loaded_integration_module(hass, "hassio.const").DATA_COMPONENT

        raw = hass.data.get(DATA_COMPONENT)
        if raw is None or raw.websession is None or raw.websession.closed:
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Core's Supervisor session is not ready",
                status="not_started",
            )
        return raw, get_supervisor_client(hass)

    def pin(self) -> None:
        raw, client = self._owners()
        if self.raw is None:
            self.raw, self.client = raw, client
        elif self.raw is not raw or self.client is not client:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The Core Supervisor owner changed during this call",
                status="not_started",
            )

    async def authorize(self) -> None:
        await authorize_actor(self.actor, self.manager.backend.authorize)
        self.pin()

    async def installed(self) -> dict[str, Any]:
        return await self._read("/addons")

    async def store(self) -> dict[str, Any]:
        return await self._read("/store")

    async def app_info(self, slug: str) -> dict[str, Any]:
        _validate_supervisor_slug(slug)
        return await self._read(f"/addons/{slug}/info")

    async def app_stats(self, slug: str) -> dict[str, Any]:
        _validate_supervisor_slug(slug)
        return await self._read(f"/addons/{slug}/stats")

    async def _read(self, endpoint: str) -> dict[str, Any]:
        await self.authorize()
        try:
            # HassIO's public reader preserves unknown Supervisor fields. Its
            # session, endpoint, credentials and decoding remain Core-owned.
            reply = await await_owned_io(
                self.raw.send_command(
                    endpoint, method="get", timeout=30, source="ha_mcp_tools"
                )
            )
        except Exception as error:  # noqa: BLE001 - Core transport errors can contain credentials
            raise ToolFailure(
                "SUPERVISOR_READ_FAILED",
                f"Supervisor read failed ({type(error).__name__})",
                status="not_started",
            ) from None
        await self.authorize()
        if (
            not isinstance(reply, dict)
            or reply.get("result") != "ok"
            or not isinstance(reply.get("data"), dict)
        ):
            raise UnsupportedOperation("Core Supervisor response envelope")
        data: dict[str, Any] = reply["data"]
        return data
