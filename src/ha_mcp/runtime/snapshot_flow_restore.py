"""Flow-backed snapshot restoration shared by explicit native and remote ports."""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Protocol

from ha_mcp.errors import HomeAssistantCommandError, HomeAssistantError

from .flows.entry import (
    CreationFlowError,
    OptionsFlowError,
    create_flow_helper,
    update_config_entry_options,
)
from .flows.subentry_restore import recreate_config_subentry, restore_config_subentry
from .snapshot_files import _flow_entry_id, _flow_label, _require_dict
from .snapshot_flow_values import (
    _apply_and_verify,
    _flow_saved_entities,
    _subentry_expectation,
    _validate_flow_snapshot,
    _verify_readback,
)
from .snapshot_payloads import (
    BackupRestoreError,
    InvalidBackupSnapshotError,
    MandatoryBackupError,
    _flow_options,
    _FlowHelperReadError,
    _subentry_data,
    _subentry_row,
    _subentry_target,
)

logger = logging.getLogger(__name__)


class FlowRestorePorts(Protocol):
    session: Any
    errors: tuple[type[BaseException], ...]

    async def helper(self, entry_id: str, helper_type: str) -> Any: ...
    async def subentry(self, target: str) -> Any: ...
    async def subentries(self, entry_id: str) -> list[dict[str, Any]]: ...
    async def entries(self) -> Any: ...
    async def preflight(
        self, entry_id: str, snapshot: dict[str, Any], helper_type: str
    ) -> list[dict[str, Any]] | None: ...
    async def restore_ids(
        self, entry_id: str, old_entry: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, str]]: ...
    async def check_collision(self, entity_id: str) -> None: ...
    def log_failure(self, step: str, error: BaseException) -> None: ...


def _flow_snapshot_for_restore(
    entity_id: str, config: Any, helper_type: str
) -> dict[str, Any]:
    try:
        return _validate_flow_snapshot(entity_id, config, helper_type)
    except InvalidBackupSnapshotError as error:
        raise BackupRestoreError(str(error), reason="invalid_snapshot") from error


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HomeAssistantError(f"Expected a list for {label}")
    return value


def _flow_failure_reason(step: str, error: BaseException) -> str:
    if isinstance(error, _FlowHelperReadError):
        return error.reason
    if isinstance(error, BackupRestoreError):
        return str(error.outcome.get("reason") or "restore_refused")
    if isinstance(error, InvalidBackupSnapshotError):
        return "invalid_snapshot"
    if isinstance(error, MandatoryBackupError):
        return "backup_capture_failed"
    if step == "safety_backup" and isinstance(error, OSError):
        return "backup_storage_failed"
    return "upstream_error"


def _flow_safe_failure_detail(step: str, error: BaseException) -> str | None:
    if isinstance(error, MandatoryBackupError):
        return error.safe_detail
    if isinstance(error, _FlowHelperReadError):
        return str(error)  # locally authored, names no option values
    if isinstance(error, HomeAssistantCommandError) and error.code:
        # HA's structured code, not its message: unknown_command is the
        # ha_mcp_tools component being absent.
        return f"{type(error).__name__} ({error.code})"
    # The safety stage wraps remote fetch failures in MandatoryBackupError;
    # a raw OSError here comes from retained-file I/O. Other stages call HA.
    if step == "safety_backup" and isinstance(error, OSError):
        return str(error)
    return None


async def _restore_flow_helper(
    ports: FlowRestorePorts, entity_id: str, config: Any, helper_type: str
) -> Any:
    """Restore a flow helper's options, or recreate an authoritatively absent one.

    The snapshot goes through the helper's options flow. A snapshot option
    none of its forms offers (fixed at creation, like a template's type) must
    still match the stored one, or the restore is refused unapplied.
    """
    label = _flow_label(helper_type)
    snapshot = _flow_snapshot_for_restore(entity_id, config, helper_type)
    entry_id = _flow_entry_id(entity_id, snapshot, helper_type)
    try:
        current = await ports.helper(entry_id, helper_type)
        if current is None:
            return await _recreate_flow_helper(ports, entry_id, snapshot, helper_type)
    except BackupRestoreError as err:
        ports.log_failure("options_preflight", err)
        raise
    except ports.errors as err:
        ports.log_failure("options_preflight", err)
        raise BackupRestoreError(
            f"{label} helper could not be checked; restore was not attempted"
        ) from err
    options = _flow_options(snapshot.get("options"), helper_type)
    return await _apply_and_verify(
        f"{label} helper",
        partial(
            update_config_entry_options,
            ports.session,
            entry_id,
            options,
            expected_domain=helper_type,
            noun="helper",
            keep_current_values=False,
            fixed_options=current["options"],
        ),
        partial(
            _verify_readback,
            partial(ports.helper, entry_id, helper_type),
            {"entry_id": entry_id, "options": options},
            ("entry_id", "options"),
            read_errors=ports.errors,
            log_failure=ports.log_failure,
        ),
    )


async def _recreate_flow_helper(
    ports: FlowRestorePorts, entry_id: str, snapshot: dict[str, Any], helper_type: str
) -> dict[str, Any]:
    """Create through the ordinary helper flow; never retry uncertain writes."""
    label = _flow_label(helper_type)
    snapshot = _flow_snapshot_for_restore(entry_id, snapshot, helper_type)
    try:
        saved = await ports.preflight(entry_id, snapshot, helper_type)
    except BackupRestoreError as err:
        ports.log_failure("recreation_preflight", err)
        raise
    except ports.errors as err:
        ports.log_failure("recreation_preflight", err)
        raise BackupRestoreError(
            f"{label} helper absence and entity IDs could not be verified; recreation was not attempted",
            reason="recreation_preflight_unavailable",
        ) from err
    try:
        result = await create_flow_helper(
            ports.session,
            helper_type,
            dict(snapshot["options"]),
            complete_snapshot=True,
        )
    except CreationFlowError as err:
        ports.log_failure("create_entry", err)
        identity: dict[str, Any] = (
            {"entry_id": err.entry_id, "entity_id": err.entry_id}
            if err.entry_id
            else {}
        )
        raise BackupRestoreError(
            str(err),
            apply_status=err.apply_status,
            reason=err.reason
            or (
                "creation_outcome_unknown"
                if err.apply_status == "unknown"
                else "creation_flow_failed"
            ),
            fields=list(err.fields),
            restore_mode="recreated",
            original_entry_id=entry_id,
            **identity,
        ) from err
    except ports.errors as err:
        ports.log_failure("create_entry", err)
        raise BackupRestoreError(
            f"{label} helper creation outcome is unknown; inspect helpers before retrying",
            apply_status="unknown",
            reason="creation_outcome_unknown",
            restore_mode="recreated",
            original_entry_id=entry_id,
        ) from err
    new_entry_id = result.get("entry_id")
    if (
        not isinstance(new_entry_id, str)
        or not new_entry_id
        or new_entry_id == entry_id
    ):
        logger.warning(
            "Flow helper restore step=create_entry reason=missing_new_identity"
        )
        raise BackupRestoreError(
            f"{label} helper creation returned no new identity; inspect helpers before retrying",
            apply_status="unknown",
            reason="creation_outcome_unknown",
            original_entry_id=entry_id,
        )
    outcome = {
        "entry_id": new_entry_id,
        "entity_id": new_entry_id,
        "original_entry_id": entry_id,
        "restore_mode": "recreated",
    }
    expected = {"entry_id": new_entry_id, "options": snapshot["options"]}
    mapping: list[dict[str, str]] = []
    try:
        verification = await _verify_readback(
            partial(ports.helper, new_entry_id, helper_type),
            expected,
            ("entry_id", "options"),
            read_errors=ports.errors,
            log_failure=ports.log_failure,
        )
        if verification != "matched":
            raise BackupRestoreError(
                f"{label} helper was recreated but its options could not be verified",
                verification_status=verification,
            )
        if saved is not None:
            mapping = await ports.restore_ids(new_entry_id, entry_id, saved)
    except ports.errors as err:
        ports.log_failure("recreation_readback", err)
        detail = (
            err.outcome
            if isinstance(err, BackupRestoreError)
            else {"verification_status": "unavailable"}
        )
        raise BackupRestoreError(
            f"{label} helper was recreated but recovery is incomplete; inspect the new entry before retrying",
            **{**detail, **outcome, "apply_status": "applied"},
        ) from err
    response = {
        **result,
        **outcome,
        "entity_ids_restored": saved is not None,
        "entity_id_mapping": mapping,
    }
    if saved is None:
        response["warnings"] = [
            "This snapshot has no entity mapping; the recreated helper may have a new entity ID."
        ]
    return response


async def _restore_config_subentry(
    ports: FlowRestorePorts, entity_id: str, config: Any, *, recreate_only: bool = False
) -> Any:
    """Restore a subentry's data through its reconfigure flow.

    A snapshot field none of the reconfigure forms offers must still match the
    stored one. When Home Assistant marks the last form (``last_step``) that
    is checked before anything is submitted; otherwise the forms are applied
    and the readback reports the mismatch.
    """
    try:
        entry_id, subentry_id = _subentry_target(entity_id)
        snapshot = _require_dict(config, "config subentry snapshot")
        if (snapshot.get("entry_id"), snapshot.get("subentry_id")) != (
            entry_id,
            subentry_id,
        ) or not isinstance(snapshot.get("subentry_type"), str):
            raise _FlowHelperReadError(
                "snapshot does not match its target", "target_mismatch"
            )
        data = _subentry_data(snapshot.get("data"))
    except HomeAssistantError as err:
        ports.log_failure("snapshot_validation", err)
        raise BackupRestoreError(
            f"Config subentry snapshot is invalid: {err}", reason="invalid_snapshot"
        ) from err
    try:
        subentries = await ports.subentries(entry_id)
        current = _subentry_row(subentries, entry_id, subentry_id)
    except ports.errors as err:
        ports.log_failure("subentry_preflight", err)
        detail = _flow_safe_failure_detail("subentry_preflight", err)
        raise BackupRestoreError(
            "Config subentry could not be checked"
            + (f" ({detail})" if detail else "")
            + "; restore was not attempted",
            reason=_flow_failure_reason("subentry_preflight", err),
        ) from err
    if current is None:
        return await _recreate_config_subentry(
            ports, entity_id, snapshot, data, subentries
        )
    if recreate_only:
        raise BackupRestoreError(
            "Config subentry reappeared after its safety capture; not restored",
            reason="entry_still_exists",
        )
    expected, keys = _subentry_expectation(snapshot, data)
    return await _apply_and_verify(
        "Config subentry",
        partial(
            restore_config_subentry,
            ports.session,
            entry_id,
            subentry_id,
            snapshot["subentry_type"],
            data,
            current["data"],
            snapshot.get("title"),
        ),
        partial(
            _verify_readback,
            partial(ports.subentry, entity_id),
            expected,
            keys,
            read_errors=ports.errors,
            log_failure=ports.log_failure,
        ),
    )


async def _recreate_config_subentry(
    ports: FlowRestorePorts,
    entity_id: str,
    snapshot: dict[str, Any],
    data: dict[str, Any],
    siblings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a deleted subentry again from its snapshot; it gets a new id.

    ``siblings`` are the entry's current subentries: one of the snapshot's type
    that already holds its data and title is an earlier recreation, so
    restoring the same snapshot twice does not add a second copy. The title
    is part of the match because sibling agents can differ by name alone.
    """
    entry_id, subentry_id = _subentry_target(entity_id)
    title = snapshot.get("title")
    recreated = next(
        (
            sub.get("subentry_id")
            for sub in siblings
            if sub.get("subentry_type") == snapshot["subentry_type"]
            and sub.get("data") == data
            and (not isinstance(title, str) or sub.get("title") == title)
        ),
        None,
    )
    if recreated:
        raise BackupRestoreError(
            f"Config subentry {subentry_id} is gone, but {entry_id}/{recreated} already holds this snapshot's data and title; a second copy was not created",
            reason="already_recreated",
            entity_id=f"{entry_id}/{recreated}",
        )
    try:
        result = await recreate_config_subentry(
            ports.session,
            entry_id,
            snapshot["subentry_type"],
            data,
            snapshot.get("title"),
        )
    except OptionsFlowError as err:
        ports.log_failure("subentry_recreation", err)
        raise BackupRestoreError(
            str(err)
            if err.reason or err.apply_status == "not_applied"
            else "Config subentry recreation did not complete normally; inspect the entry's subentries before retrying",
            apply_status=err.apply_status,
            reason=err.reason,
            fields=list(err.fields),
        ) from err
    target = f"{entry_id}/{result['subentry_id']}"
    expected, keys = _subentry_expectation(snapshot, data)
    verification = await _verify_readback(
        partial(ports.subentry, target),
        expected,
        keys,
        read_errors=ports.errors,
        log_failure=ports.log_failure,
    )
    if verification != "matched":
        raise BackupRestoreError(
            f"Config subentry was recreated as {target} but its data or title could not be verified",
            apply_status="applied",
            verification_status=verification,
            entity_id=target,
        )
    return {**result, "original_subentry_id": subentry_id, "entity_id": target}


async def _flow_recreation_preflight(
    ports: FlowRestorePorts, entry_id: str, snapshot: dict[str, Any], helper_type: str
) -> list[dict[str, Any]] | None:
    """Confirm absence independently, then reject collisions before creating."""
    _flow_options(snapshot.get("options"), helper_type)
    saved = _flow_saved_entities(snapshot)
    entries = _require_list(await ports.entries(), "config entries")
    for raw_entry in entries:
        entry = _require_dict(raw_entry, "config entry")
        if not isinstance(entry.get("entry_id"), str) or not entry["entry_id"]:
            raise BackupRestoreError("Config-entry listing is incomplete")
        if entry["entry_id"] == entry_id:
            raise BackupRestoreError(
                f"{_flow_label(helper_type)} config entry still exists; recreation was not attempted",
                reason="entry_still_exists",
            )
    for row in saved or []:
        await ports.check_collision(row["entity_id"])
    return saved
