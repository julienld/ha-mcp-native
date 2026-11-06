"""Public trace contract, independent of Core and the MCP SDK."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field


class TraceFacade:
    def __init__(self, get_traces: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.get_traces = get_traces

    async def ha_get_automation_traces(
        self,
        automation_id: Annotated[
            str,
            Field(
                description="Automation or script entity_id (e.g., 'automation.motion_light' or 'script.morning_routine')"
            ),
        ],
        run_id: Annotated[
            str | None,
            Field(
                description="Specific trace run_id to retrieve detailed trace.",
                default=None,
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                description="Maximum number of traces to return when listing (default: 10, max: 50).",
                default=10,
                ge=1,
                le=50,
            ),
        ] = 10,
        deduplicate: Annotated[
            bool,
            Field(
                description="Deduplicate variables across action steps. Set to False to record the "
                "variables at every step; steps whose variables carry 'trigger', and "
                "null-valued entries, are omitted either way.",
                default=True,
            ),
        ] = True,
        detailed: Annotated[
            bool,
            Field(
                description="Include extra diagnostic data: logbook entries and context metadata.",
                default=False,
            ),
        ] = False,
        sections: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated list of trace sections to return. "
                    "Valid values: trigger, conditions, actions, config, error, logbook, context. "
                    "Omit to return all sections. Example: 'actions' or 'trigger,conditions'."
                ),
                default=None,
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                description="Number of traces to skip from the start of the requested order. Use with `limit` to page through stored traces when `total_available > limit`.",
                default=0,
                ge=0,
            ),
        ] = 0,
        order: Annotated[
            Literal["newest", "oldest"],
            Field(
                description="Order traces are returned in. 'newest' returns most-recent first; "
                "'oldest' returns chronological-first.",
                default="newest",
            ),
        ] = "newest",
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Get execution traces for automations and scripts to debug issues.

        Traces show what triggered a run, which conditions passed or failed,
        which actions executed (for 'choose', which branch was taken), any
        errors, and variable values during execution. Home Assistant keeps
        only the most recent runs of each automation or script (its
        stored_traces setting), so older runs are not available. The 'state'
        field shows 'stopped' (completed), 'running', or an error state.

        USAGE MODES:
        1. List recent traces (omit run_id): returns a summary of recent runs
           with timestamps, triggers, and status. Use `offset` to page deeper
           when `has_more` is true.
           ha_get_automation_traces("automation.motion_light")
        2. Get a detailed trace (provide run_id): full execution details
           including trigger info, condition results, action trace with timing,
           and context variables.
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456")
        3. Add logbook entries and context metadata: detailed=True. Script-style
           action paths (sequence/, numeric) are always matched regardless of
           this flag.
        4. Variables at every step (trigger sets and null entries are still
           omitted): deduplicate=False.
        """
        return await self.get_traces(
            automation_id,
            run_id,
            limit,
            deduplicate,
            detailed,
            sections,
            offset,
            order,
            ctx,
        )
