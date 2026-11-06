"""Single public contract for native owners and isolated legacy adapters."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class ReportFacade:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def ha_report_issue(
        self,
        report_type: Annotated[
            Literal["runtime_bug", "agent_behavior", "feature_request"],
            Field(
                default="runtime_bug",
                description="'runtime_bug' when ha-mcp errored or behaved wrongly; 'agent_behavior' when the user says you used the wrong tool or worked inefficiently; 'feature_request' when the user wants ha-mcp to do something it cannot do yet.",
            ),
        ] = "runtime_bug",
        title: Annotated[
            str | None,
            Field(
                default=None,
                description="One-line summary of what broke or what is requested, for the issue title.",
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                default=None,
                description="What went wrong in markdown: steps to reproduce, expected and actual behavior. For agent_behavior: what you did and what you should have done. For feature_request: what ha-mcp should do, why the user needs it, and what you tried.",
            ),
        ] = None,
        user_prompt: Annotated[
            str | None,
            Field(
                default=None,
                description="The user message that led to the problem, verbatim.",
            ),
        ] = None,
        user_comment: Annotated[
            str | None,
            Field(
                default=None,
                description="The user's own words on what went wrong or what bothered them, verbatim. Ask the user for it; never write it yourself.",
            ),
        ] = None,
        tool_calls: Annotated[
            str | None,
            Field(
                default=None,
                description="The tool call(s) that produced the problem, verbatim: name, arguments and the (shortened) response.",
            ),
        ] = None,
        ai_model: Annotated[
            str | None,
            Field(
                default=None,
                description="Your own model identity, as specific as you know it. Do not invent a version.",
            ),
        ] = None,
        client_app: Annotated[
            str | None,
            Field(
                default=None,
                description="The app and version the user runs you in, as the USER states it (usually on the app's About screen or from its --version command). Never guess it.",
            ),
        ] = None,
        tool_call_count: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                le=16,
                description="Number of ha_* tool calls made since the issue started; determines how many log entries to include.",
            ),
        ] = 10,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="Return only the specified top-level response keys. None = full response. Typical: 'issue_title,issue_body,issue_url,duplicate_check_urls,anonymization_guide,missing_tool_hint,known_client_issues_hint,instructions'. issue_body already embeds the relevant logs, so the raw log keys are only needed for your own analysis. Available keys: diagnostic_info, recent_logs, startup_logs, addon_logs, core_error_log, log_count, startup_log_count, formatted_report, issue_title, issue_body, issue_url, anonymization_guide, duplicate_check_urls, missing_tool_hint, known_client_issues_hint, instructions.",
            ),
        ] = None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Get diagnostics and a finished GitHub issue for a bug, agent feedback or a feature request.

        Use it before filing any GitHub issue about ha-mcp: a bug report
        without this report is closed after 24 hours. `report_type` picks
        the kind of report; if it is unclear which, ask: "Are you reporting a
        bug in ha-mcp, giving feedback on how I used the tools, or requesting
        a feature?"

        Pass the report text in the call: the server combines it with the
        diagnostics it collects into `issue_title`, `issue_body` (the full
        report with logs) and `issue_url` (a new-issue link with title and
        body filled in, log sections left out to fit GitHub's URL limit;
        error messages stay in, with secrets redacted). A call
        without text still returns diagnostics, with placeholders in the body.

        The response is LARGE; `fields=` narrows it. Read `instructions`
        before showing anything to the user: it covers the missing-tool and
        known-client pre-checks, the duplicate check, the mandatory
        anonymisation step, and how to file the issue. Check
        `missing_tool_hint` FIRST when the report is about a missing tool: a
        stale client tool list (not a bug) is the usual cause.
        """
        result: dict[str, Any] = await self.service.report(
            report_type=report_type,
            title=title,
            description=description,
            user_prompt=user_prompt,
            user_comment=user_comment,
            tool_calls=tool_calls,
            ai_model=ai_model,
            client_app=client_app,
            tool_call_count=tool_call_count,
            fields=fields,
            ctx=ctx,
        )
        return result
