"""Device-operation status presentation, shared without a listener or transport."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.utils.operation_manager import DeviceOperation

from .errors import raise_failure


def operation_status_result(
    operation: DeviceOperation,
    *,
    verification_method: str = "websocket_state_change",
    monitoring: str = "websocket_state_changes",
) -> dict[str, Any]:
    operation_id = operation.operation_id
    if operation.status.value == "completed":
        return {
            "operation_id": operation_id,
            "status": "completed",
            "success": True,
            "entity_id": operation.entity_id,
            "action": operation.action,
            "final_state": operation.result_state,
            "duration_ms": operation.duration_ms,
            "message": f"Device {operation.entity_id} successfully {operation.action}",
            "verification_method": verification_method,
            "details": {
                "service_call": {
                    "domain": operation.service_domain,
                    "service": operation.service_name,
                    "data": operation.service_data,
                },
                "expected_state": operation.expected_state,
                "actual_state": operation.result_state,
            },
        }

    elif operation.status.value == "failed":
        raise_failure(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                operation.error_message or "Device operation failed",
                context={
                    "operation_id": operation_id,
                    "entity_id": operation.entity_id,
                    "action": operation.action,
                    "duration_ms": operation.duration_ms,
                },
                suggestions=[
                    "Check if device is available and responding",
                    "Verify device supports the requested action",
                    "Check Home Assistant logs for error details",
                    "Try a simpler action like toggle",
                ],
            )
        )

    elif operation.status.value == "timeout":
        raise_failure(
            create_error_response(
                ErrorCode.TIMEOUT_OPERATION,
                f"Operation timed out after {operation.timeout_ms}ms",
                context={
                    "operation_id": operation_id,
                    "entity_id": operation.entity_id,
                    "action": operation.action,
                    "elapsed_ms": operation.elapsed_ms,
                },
                suggestions=[
                    "Device may be slow to respond or offline",
                    "Check device connectivity",
                    "Try increasing timeout for slow devices",
                    "Verify device is powered on",
                ],
            )
        )

    else:  # pending
        return {
            "operation_id": operation_id,
            "status": "pending",
            "entity_id": operation.entity_id,
            "action": operation.action,
            "elapsed_ms": operation.elapsed_ms,
            "timeout_in_ms": operation.timeout_ms,
            "time_remaining_ms": operation.timeout_ms - operation.elapsed_ms,
            "message": f"Waiting for {operation.entity_id} to respond to {operation.action}...",
            "expected_state": operation.expected_state,
            "monitoring": monitoring,
            "tips": [
                "Operation will auto-complete when device state changes",
                "Physical devices may take 1-3 seconds to respond",
                "Call ha_get_operation_status again to check for updates",
            ],
        }


def bulk_status_result(
    operation_ids: list[str], statuses: list[dict[str, Any]]
) -> dict[str, Any]:
    completed = len([s for s in statuses if s.get("status") == "completed"])
    failed = len([s for s in statuses if s.get("status") in ["failed", "timeout"]])
    not_found = len([s for s in statuses if s.get("status") == "not_found"])
    pending = len([s for s in statuses if s.get("status") == "pending"])

    # The batch call itself succeeded; per-item failures live inside
    # detailed_results (batch-item pattern). Top-level ``success`` is
    # the repo-wide response contract every tool return carries.
    return {
        "success": True,
        "total_operations": len(operation_ids),
        "completed": completed,
        "failed": failed,
        "not_found": not_found,
        "pending": pending,
        "all_complete": pending == 0,
        "summary": {
            "success_rate": f"{completed}/{len(operation_ids)}",
            # Terminal fraction (nothing left in flight) — consistent
            # with all_complete; success_rate carries the successful
            # fraction separately.
            "completion_percentage": (
                (completed + failed + not_found) / len(operation_ids)
            )
            * 100,
        },
        "detailed_results": statuses,
        "recommendations": (
            [
                "Wait a few seconds and check again if operations are pending",
                "Check failed operations for specific error messages",
                "Retry failed operations with different parameters if needed",
            ]
            if pending > 0 or failed > 0 or not_found > 0
            else ["All operations completed successfully!"]
        ),
    }
