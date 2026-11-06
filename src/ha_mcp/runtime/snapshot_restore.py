"""Restore sequencing shared by native and remote domain operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol

from .snapshot_files import SnapshotFilesMixin, _is_flow_helper_domain
from .snapshot_payloads import BackupRestoreError

ApplyStatus = Literal["not_applied", "unknown", "applied"]


class RestoreOperations(Protocol):
    async def needs_safety(self) -> bool: ...

    async def capture_safety(self, outcome: dict[str, Any]) -> Path | None: ...

    async def require_safety(self, path: Path) -> None: ...

    async def apply(self, needs_safety: bool, has_safety: bool) -> Any: ...

    def log_failure(self, step: str, error: BaseException) -> None: ...

    def failed(
        self,
        error: BaseException,
        step: str,
        status: ApplyStatus,
        outcome: dict[str, Any],
    ) -> NoReturn: ...

    def completed(self, result: Any) -> dict[str, Any]: ...


async def restore_snapshot_data(
    store: SnapshotFilesMixin,
    name: str,
    data: dict[str, Any],
    operations: RestoreOperations,
    *,
    take_safety_backup: bool,
    restore_errors: tuple[type[BaseException], ...],
) -> dict[str, Any]:
    """Pin recovery, dispatch once, and retain apply knowledge across failures."""
    domain = data["domain"]
    outcome: dict[str, Any] = {
        "restored_from": name,
        "domain": domain,
        "entity_id": data["entity_id"],
        "safety_backup": None,
    }
    safety_path: Path | None = None
    status: ApplyStatus = "not_applied"
    step = "safety_preflight"
    try:
        needs_safety = await operations.needs_safety()
        flow_driven = (
            _is_flow_helper_domain(domain) or domain == "helper_config_subentry"
        )
        if needs_safety and (flow_driven or take_safety_backup):
            step = "safety_backup"
            safety_path = await operations.capture_safety(outcome)
            if safety_path is not None:
                store._protect_snapshot(safety_path.name)
                await operations.require_safety(safety_path)
                outcome["safety_backup"] = safety_path.name
        status = "unknown"
        step = "apply"
        result = await operations.apply(needs_safety, safety_path is not None)
    except BackupRestoreError as error:
        if step != "apply":
            operations.log_failure(step, error)
        error.outcome = {**outcome, **error.outcome}
        raise
    except restore_errors as error:
        operations.failed(error, step, status, outcome)
    finally:
        if safety_path is not None:
            store._unprotect_snapshot(safety_path.name)
    outcome.update(operations.completed(result))
    return {**outcome, "result": result}
