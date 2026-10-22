"""``ha_get_logs(source='fault_log')`` — Home Assistant's native-crash dump.

HA Core's ``__main__`` enables :mod:`faulthandler` on ``home-assistant.log.fault``
in the config root. faulthandler writes only on a native fatal signal (SIGSEGV,
SIGABRT, SIGBUS, SIGILL, SIGFPE, or a Python fatal error): the process dies
before any logging runs, so the per-thread Python traceback it dumps reaches
neither journald nor ``home-assistant.log``. No other ``ha_get_logs`` source can
show it (issue #2373).

The file is opened in append mode on every start, so on a healthy install it
exists and is empty; each crash appends one block that opens with a
``Fatal Python error: ...`` line followed by one traceback per thread. A block
is only readable in its original line order, so unlike the line-oriented
raw-text sources this one orders whole blocks and pages through the result
with ``offset`` instead of reversing lines.

The read goes through the File & YAML Tools entry's privileged ``read_file``
service (the same route as ``ha_read_file``), which allows the path from
component 2.2.0. Split out of ``log_sources`` under `.gemini/styleguide.md`
§ Tool Consolidation and Module Size.
"""

from typing import Any, Literal, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.runtime.fault_log_results import _FATAL_MARKER as _FATAL_MARKER
from ha_mcp.runtime.fault_log_results import _NO_CRASH_MESSAGE as _NO_CRASH_MESSAGE
from ha_mcp.runtime.fault_log_results import FAULT_LOG_PATH as FAULT_LOG_PATH
from ha_mcp.runtime.fault_log_results import (
    FAULT_LOG_WINDOW_LINES as FAULT_LOG_WINDOW_LINES,
)
from ha_mcp.runtime.fault_log_results import (
    MIN_COMPONENT_VERSION_FAULT_LOG as MIN_COMPONENT_VERSION_FAULT_LOG,
)
from ha_mcp.runtime.fault_log_results import (
    _is_missing_file_error as _is_missing_file_error,
)
from ha_mcp.runtime.fault_log_results import _no_crash as _no_crash
from ha_mcp.runtime.fault_log_results import _shape_crash_page as _shape_crash_page
from ha_mcp.runtime.fault_log_results import _split_blocks as _split_blocks

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    DEFAULT_LOG_LIMIT,
    _coerce_limit,
)
from .tools_filesystem import _assert_mcp_tools_available, call_mcp_tools_service
from .util_helpers import unwrap_service_response

# Config-relative path HA Core hands to ``faulthandler.enable`` (``FAULT_LOG_FILENAME``
# in ``homeassistant/__main__.py``).

# First component release whose read allowlist includes ``FAULT_LOG_PATH``. An
# older component answers the read with "Path not allowed"; that reply is the
# version signal, no separate probe needed.

# Lines tailed from the file per call, independent of ``limit``. A single dump
# lists every thread, so it routinely runs past a 100-line ``limit``; tailing
# only ``limit`` lines would cut off the ``Fatal Python error`` header that
# names the signal. The window is the same one the other raw-text sources use
# for a search, and ``offset`` pages through it.

# faulthandler opens every dump with this line, so it delimits crash blocks.


def _raise_read_failure(error: str) -> NoReturn:
    """Turn a ``read_file`` refusal into the matching structured error.

    "Path not allowed" is the one refusal with a known cause: a component
    older than :data:`MIN_COMPONENT_VERSION_FAULT_LOG`, whose allowlist does
    not yet carry the file. Anything else is reported verbatim.
    """
    if "Path not allowed" in error:
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "The installed ha_mcp_tools custom component does not allow "
                f"reading {FAULT_LOG_PATH} (requires component >= "
                f"{MIN_COMPONENT_VERSION_FAULT_LOG}).",
                details=error,
                suggestions=[
                    "HACS → Integrations → HA-MCP Custom Component → Update",
                    "Restart Home Assistant after the update completes",
                ],
                context={"source": "fault_log"},
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"read_file failed for {FAULT_LOG_PATH}: {error or 'unknown error'}",
            # Parenthesised so each reads as one suggestion rather than a
            # list entry with a missing comma (py/implicit-string-
            # concatenation-in-list).
            suggestions=[
                (
                    f"Check that {FAULT_LOG_PATH} in the Home Assistant config "
                    "directory is readable by Home Assistant"
                ),
                "Check Home Assistant logs for the ha_mcp_tools read error",
                (
                    "Retry after the file is readable; an absent or empty file "
                    "is reported as no crash, not as an error"
                ),
            ],
            context={"source": "fault_log"},
        )
    )


class FaultLogSourceMixin:
    """``fault_log`` source, mixed into ``LogTools``."""

    _client: Any

    async def _read_fault_file(self) -> dict[str, Any]:
        """One gated ``read_file`` call, unwrapped; transport errors become ToolErrors.

        The live tools-entry probe runs first, as the file and YAML tools do,
        so an entry removed after this client cached its caller token surfaces
        the actionable "add the entry" error rather than HA's 400 for a
        service that no longer exists. That gate and the caller-token gate
        raise their own ToolErrors, which pass through untouched.
        """
        try:
            await _assert_mcp_tools_available(self._client)
            raw = await call_mcp_tools_service(
                self._client,
                "read_file",
                {"path": FAULT_LOG_PATH, "tail_lines": FAULT_LOG_WINDOW_LINES},
            )
        except ToolError:
            raise
        except (HomeAssistantAuthError, HomeAssistantAPIError) as e:
            exception_to_structured_error(e, context={"source": "fault_log"})
        except (HomeAssistantConnectionError, TimeoutError, OSError) as e:
            exception_to_structured_error(
                e,
                context={"source": "fault_log"},
                suggestions=["Check Home Assistant connection"],
            )
        return unwrap_service_response(raw) if isinstance(raw, dict) else {}

    async def _get_fault_log(
        self,
        limit: int | None = None,
        search: str | None = None,
        offset: int = 0,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Read ``home-assistant.log.fault`` through the component's read_file.

        Always tails :data:`FAULT_LOG_WINDOW_LINES` from the file; ``order``
        arranges whole crash blocks, ``search`` keeps the blocks that mention
        the term, and ``offset``/``limit`` page through the assembled text. An absent or
        empty file is the healthy state and returns success with
        ``crash_recorded=False`` rather than an error.
        """
        effective_limit = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )
        result = await self._read_fault_file()

        data: dict[str, Any] = {
            "success": True,
            "source": "fault_log",
            "path": FAULT_LOG_PATH,
            "limit": effective_limit,
            "offset": offset,
            "order": order,
            "window_lines": FAULT_LOG_WINDOW_LINES,
        }
        if not result.get("success", False):
            error = str(result.get("error", ""))
            if _is_missing_file_error(error):
                # HA opens the file at every start, so a missing file means
                # this config dir has not been started by HA's __main__ (or
                # someone removed it). Either way: nothing to show.
                return _no_crash(data, total_lines=0)
            _raise_read_failure(error)

        content = result.get("content")
        lines = content.splitlines() if isinstance(content, str) else []
        # The component reports the untailed line count; fall back to what we
        # got when an older shape omits it.
        total_lines = result.get("total_lines")
        if not isinstance(total_lines, int):
            total_lines = len(lines)
        if isinstance(result.get("modified"), str):
            data["modified"] = result["modified"]

        if not any(line.strip() for line in lines):
            return _no_crash(data, total_lines=total_lines)
        data["total_lines"] = total_lines
        # Older blocks beyond the window are unreachable by offset; say so.
        data["window_truncated"] = bool(result.get("truncated", False))
        return _shape_crash_page(
            data,
            lines,
            limit=effective_limit,
            offset=offset,
            search=search,
            order=order,
        )
