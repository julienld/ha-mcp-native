"""Single public contracts for bulk control and owned device-operation status."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, cast

from pydantic import Field, SkipValidation

from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .bulk_params import BulkControlOperation
from .bulk_selection import BulkControlSelector


class BulkFacade:
    def __init__(
        self,
        bulk_control: Callable[..., Awaitable[dict[str, Any]]],
        get_operation_status: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self._bulk_control = bulk_control
        self._get_operation_status = get_operation_status

    async def ha_get_operation_status(
        self,
        operation_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Single operation ID, or a list of IDs for a bulk status check."
                ),
            ),
        ],
        timeout_seconds: Annotated[
            float,
            Field(
                ge=0,
                allow_inf_nan=False,
                description=(
                    "Seconds to wait for a pending operation to finish before "
                    "returning its status. 0 returns the current status at once."
                ),
            ),
            # Home Assistant's MCP client abandons any call after 10 seconds.
        ] = 8,
    ) -> dict[str, Any]:
        """
        Get the status of one or more device operations with real-time Home Assistant state verification.

        Bulk checks poll all operations concurrently under one shared
        timeout_seconds window and report per-item failures inside
        detailed_results instead of aborting the batch.

        Use this to track the operation_ids that ha_bulk_control returns.
        For current entity states, use ha_get_state instead.
        """
        return await self._get_operation_status(
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
        )

    async def ha_bulk_control(
        self,
        operations: Annotated[
            list[SkipValidation[BulkControlOperation]],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Explicit entity operations. "
                    "Each item requires exact entity_id and action. Use "
                    "action='off', not service='turn_off'."
                )
            ),
            # Declared type stays `list[...]` (not `| None`) so the generated
            # tool schema shows `operations` as always a list; `cast(Any, None)`
            # supplies the runtime default without a `| None` mypy would then
            # require type-narrowing for at every use below `selector is None`.
        ] = cast(Any, None),
        parallel: Annotated[
            bool,
            Field(
                description=(
                    "Dispatch operations concurrently (default) or one at a "
                    "time when False."
                ),
            ),
        ] = True,
        ctx: Any = None,
        selector: Annotated[
            SkipValidation[BulkControlSelector] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional exact structural scope using domain plus area_ids "
                    "and/or floor_ids, with optional exclude_entity_ids."
                )
            ),
        ] = None,
        action: Annotated[
            str | None,
            Field(description="One device action applied to every resolved leaf."),
        ] = None,
        parameters: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(description="Optional action parameters for selector mode."),
        ] = None,
        timeout_seconds: Annotated[
            float | None,
            Field(
                ge=0,
                le=60,
                allow_inf_nan=False,
                strict=True,
                description=(
                    "Selector mode only: confirmation wait in seconds for every "
                    "resolved entity. In operations mode set timeout_seconds on "
                    "each operation instead; a top-level value is rejected in "
                    "operations mode."
                ),
            ),
        ] = None,
        validate_first: Annotated[
            bool,
            Field(
                strict=True,
                description=(
                    "Selector mode only: report ENTITY_NOT_FOUND for a target "
                    "that does not exist. In operations mode set "
                    "validate_first on each operation instead; a top-level "
                    "False is rejected in operations mode."
                ),
            ),
        ] = True,
        dry_run: Annotated[
            bool,
            Field(
                strict=True,
                description="Selector mode only; True is rejected in operations mode.",
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage explicit operations or one deterministic structural bulk action.

        When NOT to use: use ``ha_call_service`` for service-specific payloads or
        backend-native group targeting, and ``ha_search`` for fuzzy name discovery.

        **Operations mode** (``operations``, no ``selector``): put every target in
        this one call. Parallel execution is the default, and invalid items are
        reported without aborting valid operations in the same batch — but a batch
        in which every item fails validation dispatches nothing and fails the call.
        A batch that targets a group/aggregate entity together with one or more of
        its own individual members also fails closed (nothing dispatched): Home
        Assistant applies the action to every member when the group is targeted
        regardless of what else is listed, so a member row cannot exclude that
        member from the group's own action. Use selector mode with
        ``exclude_entity_ids`` when a group action must exclude specific members.

        **Selector mode** (``selector`` + ``action``): use exact area or floor IDs
        when exclusions must be applied after recursively expanding generic
        aggregate membership. Resolves a frozen visible leaf set before dispatch;
        it is not transactional, so Home Assistant may still report per-leaf
        failures. A selector resolving to more than 100 entities fails closed
        instead of dispatching a partial/oversized batch — narrow it (a more
        specific area/floor, or add ``exclude_entity_ids``) and retry. Set
        ``dry_run`` to preview the resolved set without changing state.
        """
        return await self._bulk_control(
            operations=operations,
            parallel=parallel,
            ctx=ctx,
            selector=selector,
            action=action,
            parameters=parameters,
            timeout_seconds=timeout_seconds,
            validate_first=validate_first,
            dry_run=dry_run,
        )
