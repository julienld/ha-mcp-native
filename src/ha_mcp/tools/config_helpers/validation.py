"""Input validation for ha_config_set_helper parameters.

Storage-helper field values are validated by Home Assistant itself (see
``core_payload``); what remains here is the tool's own call shape.
"""

from typing import Any

from ...backends.legacy import legacy_errors_sync
from ...errors import ErrorCode, create_error_response
from ...runtime.helper_params import (
    _reject_storage_params_on_flow_helper as _shared_reject_storage_params_on_flow_helper,
)
from ...runtime.helper_params import (
    _storage_helper_params as _shared_storage_helper_params,
)
from ..helpers import (
    raise_tool_error,
    validate_identifier_not_empty,
)
from .flow import _flow_helper_error_context
from .schemas import SIMPLE_HELPER_TYPES, _simple_helper_error_context

_storage_helper_params = legacy_errors_sync(_shared_storage_helper_params)
_reject_storage_params_on_flow_helper = legacy_errors_sync(
    _shared_reject_storage_params_on_flow_helper
)


async def _validate_set_helper_action(
    client: Any,
    action: str | None,
    helper_id: str | None,
    helper_type: str,
) -> str:
    """Validate and resolve the action for ha_config_set_helper."""
    if action is not None:
        if action == "create" and helper_id is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"action='create' was passed together with helper_id={helper_id!r}. "
                    "These are contradictory: create makes a new helper, while helper_id "
                    "targets an existing one.",
                    context=(
                        _simple_helper_error_context(
                            helper_type, action=action, helper_id=helper_id
                        )
                        if helper_type in SIMPLE_HELPER_TYPES
                        else await _flow_helper_error_context(
                            client, helper_type, action=action, helper_id=helper_id
                        )
                    ),
                    suggestions=[
                        "Omit helper_id to create a new helper",
                        "Or pass action='update' to modify the existing helper at helper_id",
                    ],
                )
            )
        if action == "update" and helper_id is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "action='update' requires helper_id to identify which helper to modify.",
                    context=(
                        _simple_helper_error_context(helper_type, action=action)
                        if helper_type in SIMPLE_HELPER_TYPES
                        else await _flow_helper_error_context(
                            client, helper_type, action=action
                        )
                    ),
                    suggestions=[
                        'Pass "helper_id": "my_helper" to identify the helper',
                        "Or pass action='create' (or omit action) to create a new helper",
                    ],
                )
            )
        if action == "update" and helper_id is not None:
            validate_identifier_not_empty(
                helper_id,
                "helper_id",
                suggestions=[
                    "Pass a valid helper_id to identify the helper to update",
                    "Or omit helper_id and pass action='create' to create a new helper",
                ],
                context={"helper_type": helper_type, "action": action},
            )
        return action
    # Implicit discriminator (back-compat).
    if helper_id is not None:
        validate_identifier_not_empty(
            helper_id,
            "helper_id",
            suggestions=[
                "Omit helper_id entirely to create a new helper",
                "Pass a valid helper_id to update an existing helper",
                "Or pass action='create' / action='update' explicitly to declare intent",
            ],
            context={"helper_type": helper_type},
        )
    return "update" if helper_id else "create"
