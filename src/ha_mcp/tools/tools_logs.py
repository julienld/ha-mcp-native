"""``ha_get_logs`` — one tool over every Home Assistant log source.

Holds the source dispatch, the cross-source parameter rules, and the tool
registration. The sources themselves are mixed in from ``log_sources`` (Core)
and ``log_sources_supervisor`` (Supervisor), their shared plumbing lives in
``log_common``, and the error-log window arithmetic in ``error_log_parsing``.

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size.
"""

from typing import Any

from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors
from ha_mcp.runtime.log_query import LogQuery
from ha_mcp.runtime.log_tools import LogsFacade

from .helpers import log_tool_usage, register_tool_methods
from .log_sources import CoreLogSourcesMixin
from .log_sources_fault import FaultLogSourceMixin
from .log_sources_logbook import LogbookLogSourceMixin
from .log_sources_supervisor import SupervisorLogSourcesMixin
from .tool_hints import read_only_hints


class LogTools(
    LogQuery,
    LogbookLogSourceMixin,
    CoreLogSourcesMixin,
    SupervisorLogSourcesMixin,
    FaultLogSourceMixin,
):
    """Dispatches ``ha_get_logs`` to one log source and shapes the response."""

    def __init__(self, client: Any) -> None:
        self._client = client

    get_logs = legacy_errors(LogQuery.get_logs)


class LegacyLogsFacade(LogsFacade):
    ha_get_logs = tool(
        name="ha_get_logs",
        tags={"History & Statistics"},
        annotations=read_only_hints("Get Logs", open_world=False),
    )(log_tool_usage(legacy_errors(LogsFacade.ha_get_logs)))


def register_logs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    register_tool_methods(mcp, LegacyLogsFacade(LogTools(client).get_logs))
