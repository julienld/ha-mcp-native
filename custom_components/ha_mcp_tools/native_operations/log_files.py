"""Bounded log reads using HA's configured files and existing Supervisor session."""

import asyncio
import re
from pathlib import Path
from typing import Any

from ha_mcp.runtime.fault_log_results import FAULT_LOG_PATH, FAULT_LOG_WINDOW_LINES
from ha_mcp.runtime.log_common import SYSTEM_SERVICE_SLUGS
from ha_mcp.runtime.log_window import (
    ErrorLogPage,
    _container_error_log_page,
    _journald_error_log_page,
    _journald_range_header,
)
from ha_mcp.tool_api import ToolFailure, UnsupportedOperation
from ha_mcp.utils.owned_executor import await_owned_io

from ..optional_integrations import loaded_integration_module
from .registry_access import authorize_actor


class NativeLogFiles:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def error_log(self, lines: int, offset: int) -> ErrorLogPage:
        from homeassistant.const import KEY_DATA_LOGGING

        hass = self.service.hass
        if "hassio" in hass.config.components:
            async with asyncio.timeout(30):
                return await _journald_error_log_page(lines, offset, self._core_window)
        filename = hass.data.get(KEY_DATA_LOGGING)
        if filename is None:
            raise UnsupportedOperation("Core has no configured error log file")

        def read() -> ErrorLogPage:
            from .. import _read_log_tail_sync

            result = _read_log_tail_sync(Path(filename), lines + offset + 2)
            if result.get("_error"):
                raise ToolFailure(
                    "RESOURCE_NOT_FOUND",
                    "Core's configured error log file is not readable",
                    status="not_started",
                )
            page = _container_error_log_page(result["content"], lines, offset)
            return ErrorLogPage(page.text, page.has_more or result["truncated"])

        return await await_owned_io(hass.async_add_executor_job(read))

    async def fault_log(self) -> dict[str, Any]:
        from .. import _is_path_allowed_for_read, _read_log_tail_sync

        hass = self.service.hass
        if not hass.services.has_service("ha_mcp_tools", "read_file"):
            raise ToolFailure(
                "COMPONENT_NOT_INSTALLED",
                "The HA-MCP File & YAML Tools entry must be enabled to read the native crash log.",
                status="not_started",
            )

        def read() -> dict[str, Any]:
            config_dir = Path(hass.config.config_dir)
            if not _is_path_allowed_for_read(config_dir, FAULT_LOG_PATH):
                raise ToolFailure(
                    "AUTH_FORBIDDEN",
                    "The native crash log resolves outside its allowed location",
                    status="not_started",
                )
            return _read_log_tail_sync(
                config_dir / FAULT_LOG_PATH, FAULT_LOG_WINDOW_LINES
            )

        result: dict[str, Any] = await await_owned_io(hass.async_add_executor_job(read))
        if result.get("_error") == "not_found":
            return {"content": "", "total_lines": 0, "truncated": False}
        if result.get("_error"):
            raise ToolFailure(
                "SERVICE_CALL_FAILED",
                "The native crash log could not be read",
                status="not_started",
            )
        return result

    async def _core_window(self, lines: int, offset: int) -> str:
        return await self.supervisor("core", None, lines, offset=offset)

    async def supervisor(
        self, service: str | None, addon: str | None, lines: int, *, offset: int = 0
    ) -> str:
        """aiohasupervisor 0.6 lacks Range; borrow only its existing transport.

        This private seam is limited to log endpoints. Core owns the endpoint,
        token, session and status decoding; none is reconstructed or logged.
        """
        from aiohttp import ClientTimeout
        from yarl import URL

        if addon is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", addon):
                raise ToolFailure(
                    "VALIDATION_INVALID_PARAMETER",
                    "Invalid app log slug",
                    status="not_started",
                )
            path = f"addons/{addon}/logs"
        elif service in SYSTEM_SERVICE_SLUGS:
            path = f"{service}/logs"
        else:
            raise UnsupportedOperation("Supervisor log endpoint")
        if "hassio" not in self.service.hass.config.components:
            raise UnsupportedOperation("Supervisor is not loaded on this installation")
        get_supervisor_client = loaded_integration_module(
            self.service.hass, "hassio"
        ).get_supervisor_client

        transport = get_supervisor_client(self.service.hass)._client
        if transport.session is None or transport.session.closed:
            raise UnsupportedOperation("Core Supervisor session is unavailable")
        await authorize_actor(self.service.actor, self.service.authorize)
        try:
            async with (
                asyncio.timeout(30),
                transport.session.request(
                    "GET",
                    URL(transport.api_host).joinpath(path),
                    params={"no_colors": "true"},
                    headers={
                        "Authorization": f"Bearer {transport.token}",
                        "Accept": "text/plain",
                        "Range": _journald_range_header(lines, offset),
                    },
                    timeout=ClientTimeout(total=30),
                    allow_redirects=False,
                ) as response,
            ):
                await transport._raise_on_status(response)
                if response.status not in {200, 206}:
                    raise RuntimeError("Unexpected Supervisor log status")
                text: str = await response.text()
        except Exception as error:  # noqa: BLE001 - transport errors may embed credential headers
            # Client exception objects can hold request headers. The public
            # failure only identifies the failure class, never those objects.
            raise ToolFailure(
                "SUPERVISOR_LOG_FAILED",
                f"Supervisor log read failed ({type(error).__name__})",
                status="not_started",
            ) from None
        await authorize_actor(self.service.actor, self.service.authorize)
        return text
