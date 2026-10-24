"""Shared snapshot identity checks and flow apply/readback outcomes."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ha_mcp.backup_diff import _compute_json_patch
from ha_mcp.errors import HomeAssistantError
from ha_mcp.tool_api import ToolFailure

from .snapshot_files import _flow_entry_id, _flow_label, _require_dict
from .snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
    _flow_options,
)

logger = logging.getLogger(__name__)


def _validate_flow_snapshot(
    entity_id: str, config: Any, helper_type: str
) -> dict[str, Any]:
    """Reject corrupt stored data before accessing HA or making a change."""
    try:
        _flow_entry_id(entity_id, config, helper_type)
        snapshot = _require_dict(config, f"{helper_type} helper snapshot")
        _flow_options(snapshot.get("options"), helper_type)
    except HomeAssistantError as err:
        # These validators produce local messages without snapshot values.
        raise InvalidBackupSnapshotError(
            f"{_flow_label(helper_type)} helper snapshot is invalid: {err}"
        ) from err
    return snapshot


def _snapshot_configs_match(expected: Any, current: Any) -> bool:
    """Reuse the preview's type-sensitive comparison (False is not 0)."""
    patch: list[dict[str, Any]] = []
    _compute_json_patch(expected, current, 1, patch)
    return not patch


async def _apply_and_verify(
    label: str,
    apply: Callable[[], Awaitable[Any]],
    verify: Callable[[], Awaitable[Literal["matched", "mismatched", "unavailable"]]],
) -> Any:
    """Run a flow-driven restore, then confirm it by reading the result back."""
    from .flows.entry import OptionsFlowError

    try:
        result = await apply()
    except OptionsFlowError as err:
        logger.warning(
            "Flow helper restore step=flow failed: %s apply_status=%s reason=%s "
            "fields=%s cause=%s",
            type(err).__name__,
            err.apply_status,
            err.reason,
            list(err.fields),
            type(err.__cause__).__name__ if err.__cause__ else None,
        )
        if err.apply_status == "not_applied":
            raise BackupRestoreError(
                str(err)
                if err.reason
                else f"{label} flow refused the restore; nothing was applied",
                reason=err.reason,
                fields=list(err.fields),
            ) from err
        raise BackupRestoreError(
            f"{label} restore did not complete normally; inspect it before retrying",
            apply_status=err.apply_status,
            verification_status=await verify(),
            reason=err.reason,
            fields=list(err.fields),
        ) from err
    verification = await verify()
    if verification != "matched":
        outcome = (
            "verification is unavailable"
            if verification == "unavailable"
            else "verification did not match the snapshot"
        )
        raise BackupRestoreError(
            f"{label} restore was applied but {outcome}; inspect it before retrying",
            apply_status="applied",
            verification_status=verification,
        )
    return result


def _flow_saved_entities(snapshot: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The entities a flow-helper snapshot recorded for its config entry.

    Each must derive its unique_id from the entry (see _recreated_unique_id),
    or the recreated entity it maps to cannot be found.
    """
    entities = snapshot.get("entities")
    if entities is None or entities == []:
        return None  # Older snapshots captured options without registry metadata.
    if not isinstance(entities, list):
        raise BackupRestoreError(
            "Helper snapshot has an invalid entity mapping", reason="invalid_snapshot"
        )
    seen: set[str] = set()
    for row in entities:
        entity_id = row.get("entity_id") if isinstance(row, dict) else None
        if (
            not isinstance(entity_id, str)
            or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity_id)
            or entity_id in seen
            or not isinstance(row.get("unique_id"), str)
            or snapshot["entry_id"] not in row["unique_id"]
            or any(
                row.get(key) is not None and not isinstance(row[key], str)
                for key in ("name", "original_name")
            )
        ):
            raise BackupRestoreError(
                "Helper snapshot has an unsupported entity mapping",
                reason="invalid_snapshot",
            )
        seen.add(entity_id)
    return entities


def _subentry_expectation(
    snapshot: dict[str, Any], data: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """What a restored subentry must read back: its data, and its title when
    the snapshot recorded one (a flow may keep the name there, not in data)."""
    title = snapshot.get("title")
    if isinstance(title, str):
        return {"data": data, "title": title}, ("data", "title")
    return {"data": data}, ("data",)


async def _verify_readback(
    read: Callable[[], Awaitable[Any]],
    expected: Any,
    keys: tuple[str, ...],
    *,
    read_errors: tuple[type[BaseException], ...] = (
        HomeAssistantError,
        OSError,
        TimeoutError,
        ConnectionError,
        ToolFailure,
    ),
    log_failure: Callable[[str, BaseException], None] | None = None,
) -> Literal["matched", "mismatched", "unavailable"]:
    """Bound readback after a dispatched apply, including uncertain replies."""
    try:
        async with asyncio.timeout(5):
            restored = await read()
    except read_errors as err:
        if log_failure is not None:
            log_failure("readback", err)
        return "unavailable"
    actual = {key: restored[key] for key in keys} if restored is not None else None
    if _snapshot_configs_match(expected, actual):
        return "matched"
    logger.warning("Flow helper restore step=readback reason=mismatch")
    return "mismatched"
