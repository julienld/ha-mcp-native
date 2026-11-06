"""The single declared schema for rows in a bulk device-control call."""

import logging
from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

from ha_mcp.errors import create_validation_error
from ha_mcp.tools.coercion import JSON_STRING_COERCION, parse_json_param

from .errors import raise_failure


class BulkControlOperation(TypedDict):
    """One entity action in a ha_bulk_control request."""

    entity_id: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact Home Assistant entity ID, e.g. 'light.kitchen'.",
        ),
    ]
    action: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Device action such as 'on', 'off', or 'toggle'. For lights, use "
                "'off' instead of the ha_call_service form 'turn_off'."
            ),
        ),
    ]
    parameters: NotRequired[
        Annotated[
            dict[str, Any],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional action parameters, e.g. {'brightness_pct': 30} "
                    "when action='on'. Each domain has a fixed allowlist of "
                    "supported keys; keys outside it are ignored rather than "
                    "rejected. Use ha_call_service for parameters this tool "
                    "does not carry."
                )
            ),
        ]
    ]
    timeout_seconds: NotRequired[
        Annotated[
            float,
            Field(
                ge=0,
                allow_inf_nan=False,
                # ``strict`` for the same reason validate_first carries it:
                # lax mode coerces ``true`` to 1.0, so a bool would land
                # downstream as a silent one-second timeout.
                strict=True,
                description=(
                    "Optional confirmation timeout. On the component path, all "
                    "operations share the maximum requested wait (default 10s, "
                    "capped at 60s); 0 disables confirmation waiting."
                ),
            ),
        ]
    ]
    validate_first: NotRequired[
        Annotated[
            bool,
            Field(
                strict=True,
                description=(
                    "Report an ENTITY_NOT_FOUND failure when the target entity "
                    "does not exist; default true. On the component batch path "
                    "this is detected from the captured pre-state rather than by "
                    "preventing dispatch. The action is always validated."
                ),
            ),
        ]
    ]


# Pydantic reads this runtime config when generating the TypedDict schema. Keep it
# outside the class body because mypy permits only field declarations there.
BulkControlOperation.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]


logger = logging.getLogger(__name__)
_BULK_CONTROL_OPERATION_ADAPTER = TypeAdapter(BulkControlOperation)


def _parse_bulk_operations(operations: Any) -> list[Any]:
    """Parse explicit bulk rows while preserving per-row runtime failures."""
    try:
        parsed_operations = parse_json_param(operations, "operations")
    except ValueError as exc:
        raise_failure(
            create_validation_error(
                f"Invalid operations parameter: {exc}",
                parameter="operations",
                invalid_json=True,
            )
        )
    if not isinstance(parsed_operations, list):
        raise_failure(
            create_validation_error(
                "Operations parameter must be a list",
                parameter="operations",
                details=f"Received type: {type(parsed_operations).__name__}",
            )
        )
    operations_list: list[Any] = []
    for index, operation in enumerate(parsed_operations):
        try:
            operations_list.append(
                _BULK_CONTROL_OPERATION_ADAPTER.validate_python(operation)
            )
        except ValidationError as exc:
            # include_input=False: a malformed row can carry a lock or alarm
            # code in its `parameters`, and str(exc) would otherwise write
            # that value to persistent logs.
            logger.warning(
                "ha_bulk_control operation %d failed schema validation: %s",
                index,
                exc.errors(include_url=False, include_input=False),
            )
            # The malformed row is deliberately preserved (not dropped) so
            # the runtime batch validator downstream still reports it as a
            # per-operation failure with its own diagnostics, instead of it
            # silently vanishing from the batch.
            operations_list.append(operation)
    return operations_list
