"""Bind the diagnostic report contract to actual Core and manager observations."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.report_tools import ReportFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.issue_report import NativeIssueReport


def report_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        return await ReportFacade(NativeIssueReport(manager, actor)).ha_report_issue(
            **arguments
        )

    return define_tool(
        manager.backend,
        ReportFacade,
        "ha_report_issue",
        handler=invoke,
        annotations=read_only_hints("Report Issue or Feedback", open_world=False),
        tags=frozenset({"Utilities"}),
        source_module="tools_bug_report",
        meta={
            "ha_mcp": {
                "native_support": {
                    "note": "Native reports use bounded Engine operation records and Core's configured error log. A root-logger startup collector is not installed; missing timestamps, durations and client application identity are reported as unavailable. Reports are returned for review and are never published automatically."
                }
            }
        },
    )
