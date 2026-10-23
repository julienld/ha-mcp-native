"""Log sources served by ``ha_get_logs`` from Home Assistant itself.

``system_log``, the raw/structured error log, and the per-integration logger
levels. The logbook lives in ``log_sources_logbook``; the Supervisor-backed
sources are in ``log_sources_supervisor``. Mixed into ``LogTools``
(``tools_logs``) rather than standing alone: every fetcher here is a thin shell
around one client call, and the dispatch, validation and tool registration they
share live with the host class.

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size.
"""

from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.runtime.log_results import (
    _SYSTEM_LOG_MESSAGE_CAP,
    LogResults,
)

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .error_log_parsing import (
    _error_log_window_lines,
)
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    _addon_auth_error_suggestions,
    _coerce_limit,
)


class CoreLogSourcesMixin(LogResults):
    """The log sources HA Core serves directly.

    ``_client`` is supplied by the host class (``LogTools``); this mixin adds
    no state of its own.
    """

    _client: Any

    async def _get_system_log(
        self,
        limit: int | None = None,
        search: str | None = None,
        level: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        compact: bool = True,
    ) -> dict[str, Any]:
        """Fetch structured system log entries via system_log/list."""
        effective_limit = _coerce_limit(limit)

        try:
            result = await self._client.send_websocket_message(
                {"type": "system_log/list"}
            )

            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to retrieve system log"),
                        suggestions=["Check Home Assistant connection"],
                    )
                )

            entries = result.get("result", [])
            return self.system_log_result(
                entries, effective_limit, search, level, order, compact
            )

        except ToolError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "system"},
                suggestions=[
                    "Check Home Assistant WebSocket connection",
                    "Verify system_log integration is enabled",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _get_error_log(
        self,
        limit: int | None = None,
        search: str | None = None,
        level: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        offset: int = 0,
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a bounded window of error log text (home-assistant.log, or journald).

        Container/pip installs read the plain ``home-assistant.log`` file;
        Supervisor-backed installs read HA Core's journald stream instead.
        Either way only the requested window is fetched, and ``offset`` pages
        deeper into the history (#2279) — in file lines on container installs,
        in journald entries on Supervisor-backed ones.

        With ``structured=True`` the raw text is collapsed into a counted,
        component-grouped summary instead (see ``_parse_error_log_structured``);
        ``limit``/``order`` do not apply in that mode (the summary ranks the
        whole fetched window by occurrence count rather than returning a
        positional slice of it), and ``top_n`` bounds it instead.
        """
        try:
            fetch_lines = _error_log_window_lines(limit, search, level, structured)
            page = await self._client.get_error_log(lines=fetch_lines, offset=offset)
            return self.error_log_result(
                page,
                fetch_lines,
                limit,
                search,
                level,
                order,
                offset,
                structured,
                top_n,
            )

        except ToolError:
            raise
        except HomeAssistantAuthError as e:
            # AuthError is a sibling of HomeAssistantAPIError, not a subclass,
            # so the tuple below never catches it and a 401 would propagate raw
            # to FastMCP without a structured `code`. All three fetch branches
            # (addon Supervisor, hassio proxy, /api/error_log) can raise it.
            exception_to_structured_error(
                e,
                context={"source": "error_log"},
                suggestions=_addon_auth_error_suggestions(),
            )
            raise  # unreachable: exception_to_structured_error always raises
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "error_log"},
                suggestions=[
                    "Check Home Assistant connection",
                    "The error log may be empty if no errors have occurred",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _get_logger_info(
        self,
        limit: int | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Fetch per-integration log levels via the ``logger/log_info`` WS command."""
        effective_limit = _coerce_limit(limit)

        try:
            result = await self._client.send_websocket_message(
                {"type": "logger/log_info"}
            )

            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to retrieve logger info"),
                        suggestions=[
                            "Verify the 'logger' integration is enabled in Home Assistant",
                            "Check Home Assistant WebSocket connection",
                        ],
                    )
                )

            raw_entries = result.get("result", [])
            return self.logger_result(raw_entries, effective_limit, search)

        except ToolError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "logger"},
                suggestions=[
                    "Check Home Assistant WebSocket connection",
                    "Verify the 'logger' integration is enabled",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises


__all__ = ["CoreLogSourcesMixin", "_SYSTEM_LOG_MESSAGE_CAP"]
