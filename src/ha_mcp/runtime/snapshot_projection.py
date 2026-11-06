"""Public snapshot comparison and restore errors, independent of transport."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .snapshot_payloads import BackupRestoreError, restore_error_code


def panel_diff(name: str, snapshot: dict[str, Any], current: Any) -> dict[str, Any]:
    import difflib

    import yaml  # type: ignore[import-untyped]

    backup_yaml = yaml.safe_dump(
        snapshot.get("config"), default_flow_style=False, sort_keys=True
    ).splitlines()
    current_yaml = yaml.safe_dump(
        current, default_flow_style=False, sort_keys=True
    ).splitlines()
    diff = difflib.unified_diff(
        backup_yaml,
        current_yaml,
        fromfile=f"backup:{name}",
        tofile=f"current:{snapshot['entity_id']}",
        lineterm="",
    )
    return {
        "success": True,
        "diff": "\n".join(diff),
        "backup_present": current is not None,
    }


def restore_error(error: BackupRestoreError) -> tuple[dict[str, Any], int]:
    code = restore_error_code(error)
    payload = create_error_response(
        code,
        str(error),
        context={"data": error.outcome},
        suggestions=[
            "Inspect the current configuration and restore outcome before retrying"
        ]
        + (
            ["Use safety_backup to inspect or restore the captured previous state"]
            if error.outcome.get("safety_backup")
            else []
        ),
    )
    return payload, {
        ErrorCode.RESOURCE_NOT_FOUND: 404,
        ErrorCode.ENTITY_NOT_FOUND: 404,
        ErrorCode.VALIDATION_INVALID_PARAMETER: 400,
    }.get(code, 409)
