"""Device operation IDs are private to one manager and user/client pair."""

import asyncio
import importlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


@pytest.fixture
def operations(native: Any) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.device_operations"
    )
    return module.NativeDeviceOperations()


def _reserve(store: Any, actor: CallContext | None = None) -> str:
    return store.reserve(
        actor or CallContext("actor", client_id="client"),
        [{"entity_id": "light.one", "action": "on"}],
        [
            {
                "domain": "light",
                "service": "turn_on",
                "service_data": {"entity_id": "light.one"},
            }
        ],
    )[0]


def _confirmed() -> dict[str, Any]:
    return {
        "confirmed": True,
        "outcome": "confirmed",
        "attempted": True,
        "transitions": [
            {
                "entity_id": "light.one",
                "new_state": {"state": "on", "attributes": {"future": 42}},
            }
        ],
    }


async def test_operation_ids_are_not_shared_between_users_clients_or_managers(
    operations: Any,
) -> None:
    identifier = _reserve(operations)
    operations.finish(identifier, _confirmed())
    for actor in (
        CallContext("other", client_id="client"),
        CallContext("actor", client_id="other"),
    ):
        with pytest.raises(ToolFailure) as caught:
            await operations.status(actor, AsyncMock(), identifier, 0)
        assert caught.value.code == "RESOURCE_NOT_FOUND"
        assert "light.one" not in str(caught.value.data)
    other = type(operations)()
    with pytest.raises(ToolFailure, match="not found"):
        await other.status(
            CallContext("actor", client_id="client"), AsyncMock(), identifier, 0
        )


async def test_completed_status_uses_shared_shape_and_actual_core_state(
    operations: Any,
) -> None:
    identifier = _reserve(operations)
    operations.finish(identifier, _confirmed())
    value = await operations.status(
        CallContext("actor", client_id="client"), AsyncMock(), identifier, 0
    )
    assert identifier.startswith("device-") and value["operation_id"] == identifier
    assert value["status"] == "completed" and value["success"]
    assert value["final_state"] == {"state": "on", "attributes": {"future": 42}}
    assert value["verification_method"] == "core_state_change"
    assert value["details"]["service_call"]["service"] == "turn_on"
    value["final_state"]["attributes"]["future"] = "caller changed it"
    again = await operations.status(
        CallContext("actor", client_id="client"), AsyncMock(), identifier, 0
    )
    assert again["final_state"]["attributes"]["future"] == 42


async def test_status_revalidates_after_wait_before_disclosing_the_result(
    operations: Any,
) -> None:
    identifier = _reserve(operations)
    actor = CallContext("actor", client_id="client")
    started = asyncio.Event()
    allowed = True

    async def authorize(_actor: CallContext) -> None:
        if not allowed:
            raise PermissionError("Actor revoked")
        started.set()

    task = asyncio.create_task(operations.status(actor, authorize, identifier, 1))
    await started.wait()
    await asyncio.sleep(0)
    allowed = False
    operations.finish(identifier, _confirmed())
    with pytest.raises(ToolFailure) as caught:
        await task
    assert caught.value.code == "AUTH_FORBIDDEN"
    assert "future" not in str(caught.value.data)


async def test_uncertain_batch_result_is_not_reported_as_a_retryable_failed_dispatch(
    operations: Any,
) -> None:
    identifier = _reserve(operations)
    operations.finish(
        identifier,
        {"attempted": True, "outcome": "outcome_unknown", "error": "provider failed"},
    )
    with pytest.raises(ToolFailure) as caught:
        await operations.status(
            CallContext("actor", client_id="client"), AsyncMock(), identifier, 0
        )
    error = caught.value
    assert error.code == "OUTCOME_UNKNOWN" and error.status == "confirmed"
    assert (
        error.data["operation_outcome"] == "outcome_unknown" and error.data["attempted"]
    )
    assert "Inspect" in str(error) and "retry" not in str(error.data).lower()


async def test_bulk_status_waits_on_one_window_and_preserves_sibling_outcomes(
    operations: Any,
) -> None:
    good, bad = _reserve(operations), _reserve(operations)
    operations.finish(
        bad,
        {
            "attempted": False,
            "error": "Denied before dispatch",
            "error_code": "READ_ONLY",
        },
    )
    actor = CallContext("actor", client_id="client")
    task = asyncio.create_task(
        operations.status(actor, AsyncMock(), [good, bad, "missing"], 1)
    )
    await asyncio.sleep(0)
    operations.finish(good, _confirmed())
    result = await task
    assert result["completed"] == result["failed"] == result["not_found"] == 1
    assert result["all_complete"] and result["summary"]["completion_percentage"] == 100
    assert result["detailed_results"][1]["operation_outcome"] == "not_started"


async def test_cancelled_status_wait_does_not_cancel_the_shared_operation(
    operations: Any,
) -> None:
    identifier = _reserve(operations)
    actor = CallContext("actor", client_id="client")
    task = asyncio.create_task(operations.status(actor, AsyncMock(), [identifier], 30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not operations.entries[identifier].done.is_set()
    operations.finish(identifier, _confirmed())
    assert (await operations.status(actor, AsyncMock(), identifier, 0))["success"]


def test_capacity_cannot_discard_pending_operations_but_reclaims_terminal_history(
    operations: Any,
) -> None:
    operations.max_entries = 1
    identifier = _reserve(operations)
    with pytest.raises(ToolFailure) as caught:
        _reserve(operations)
    assert caught.value.code == "OWNED_CAPACITY" and identifier in operations.entries
    operations.finish(identifier, _confirmed())
    operations.entries[identifier].terminal_at = -1_000_000
    replacement = _reserve(operations)
    assert replacement != identifier and identifier not in operations.entries


async def test_multiple_confirmed_targets_keep_every_state_in_operation_status(
    operations: Any,
) -> None:
    actor = CallContext("actor")
    identifier = operations.reserve(
        actor,
        [{"entity_id": "light.one,light.two", "action": "on"}],
        [
            {
                "domain": "light",
                "service": "turn_on",
                "service_data": {"entity_id": "light.one,light.two"},
            }
        ],
    )[0]
    operations.finish(
        identifier,
        {
            "attempted": True,
            "outcome": "confirmed",
            "confirmed": True,
            "transitions": [
                {"entity_id": name, "new_state": {"state": "on"}}
                for name in ("light.one", "light.two")
            ],
        },
    )
    result = await operations.status(actor, AsyncMock(), identifier, 0)
    assert result["status"] == "completed" and result["final_state"] is None
    assert result["final_states"] == {
        "light.one": {"state": "on"},
        "light.two": {"state": "on"},
    }
