"""Shared projections trust backend outcome types and preserve their diagnostics."""

import copy
from typing import Any

import pytest

from ha_mcp.runtime.actions import ServiceCallTools
from ha_mcp.runtime.service_outcome import (
    is_service_outcome_unknown,
    record_service_failure,
)
from ha_mcp.tool_api import CallContext, ToolFailure


@pytest.mark.parametrize("status,unknown", [(None, False), ("outcome_unknown", True)])
def test_backend_status_is_authoritative_over_payload_marker(
    status: str | None, unknown: bool
) -> None:
    error = ToolFailure(
        "SERVICE_CALL_FAILED",
        "handler diagnostic",
        data={"write_outcome_unknown": not unknown},
        status=status,
    )
    assert is_service_outcome_unknown(error) is unknown


@pytest.mark.parametrize(
    "diagnostic",
    [None, "provider text", {"code": "OWNER_ERROR", "message": "effect happened"}],
)
def test_saved_config_keeps_backend_unknown_and_diagnostic_without_mutating_error(
    diagnostic: object,
) -> None:
    payload = {"error": diagnostic, "http_status": 500}
    original = copy.deepcopy(payload)
    error = ToolFailure(
        "OWNER_ERROR", "effect happened", data=payload, status="outcome_unknown"
    )
    response = {"success": True, "config_hash": "saved"}

    record_service_failure(response, error, field="activated", entity_id="scene.test")

    assert response["success"] and response["config_hash"] == "saved"
    assert response["activated"] is None and response["write_outcome_unknown"] is True
    item = response["service_errors"][0]
    assert item["http_status"] == 500 and item["entity_id"] == "scene.test"
    assert item["error"]["message"] == "effect happened"
    assert "before retrying" in item["error"]["suggestion"]
    assert payload == original


async def test_shared_service_contract_cannot_report_ambiguous_backend_as_success() -> (
    None
):
    class Backend:
        async def authorize(self, context: CallContext) -> None:
            pass

        async def execute(
            self, operation: str, arguments: dict[str, Any], context: CallContext
        ) -> Any:
            if operation == "services.policy":
                return None
            assert operation == "services.call_observe"
            return {"unknown_outcome": True}

    tools = ServiceCallTools(Backend(), CallContext("actor"))
    with pytest.raises(ToolFailure) as caught:
        await tools.ha_call_service(domain="light", service="turn_on", wait=False)
    assert caught.value.status == "outcome_unknown"
    assert caught.value.data["write_outcome_unknown"] is True
