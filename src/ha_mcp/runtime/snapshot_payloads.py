"""Restorable flow and subentry snapshot values, independent of HA transports."""

from __future__ import annotations

from typing import Any, Literal

from ha_mcp.errors import HomeAssistantError

from .snapshot_files import _flow_label

_REGISTRY_READONLY_KEYS = frozenset({"created_at", "modified_at"})


def _strip_readonly(config: dict[str, Any], *extra: str) -> dict[str, Any]:
    """Remove universal and domain-owned keys before a Core config write."""
    drop = _REGISTRY_READONLY_KEYS | set(extra)
    return {key: value for key, value in config.items() if key not in drop}


def validate_snapshot_identity(data: dict[str, Any]) -> None:
    """Validate stored identities before selecting a semantic domain operation."""
    for key in ("domain", "entity_id", "config"):
        if key not in data:
            raise InvalidBackupSnapshotError(f"Snapshot is missing {key}")
    for key in ("domain", "entity_id"):
        if not isinstance(data[key], str) or (key == "domain" and not data[key]):
            raise InvalidBackupSnapshotError(f"Snapshot has invalid {key}")


class _FlowHelperReadError(HomeAssistantError):
    """A local validation failure with a safe diagnostic code."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class _FlowHelperBackupSkip(_FlowHelperReadError):
    """Nothing to back up: no stored or disclosed options, or no helper flow Core
    lists. Best-effort capture logs it at INFO."""


def _flow_options(config: Any, helper_type: str) -> dict[str, Any]:
    """Require persisted flow-helper options that can safely be restored."""
    from .redaction import sentinel_option_keys

    label = _flow_label(helper_type)
    if not isinstance(config, dict):
        raise _FlowHelperReadError(
            f"{label} helper options must be an object", "invalid_options"
        )
    if not config:
        raise _FlowHelperBackupSkip(  # e.g. otp: all of it is in entry.data
            f"{label} helper has no stored options to back up or restore",
            "invalid_options",
        )
    # The component's resolved-!secret scrub predates the server sentinels.
    # Neither kind of placeholder is a usable recovery value.
    if sentinel_option_keys(config) or _contains_redacted_leaf(config):
        raise _FlowHelperReadError(
            f"{label} helper options contain redacted values; capture is incomplete",
            "redacted_options",
        )
    return config


def _contains_redacted_leaf(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_redacted_leaf(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redacted_leaf(item) for item in value)
    return isinstance(value, str) and value == "**redacted**"


def _subentry_target(entity_id: str) -> tuple[str, str]:
    """``<entry_id>/<subentry_id>``, the id a config_subentry backup is keyed by."""
    entry_id, _, subentry_id = entity_id.partition("/")
    if not entry_id or not subentry_id:
        raise _FlowHelperReadError(
            "Config subentry backups are keyed by <entry_id>/<subentry_id>",
            "invalid_target",
        )
    return entry_id, subentry_id


def _subentry_data(data: Any) -> dict[str, Any]:
    from .redaction import sentinel_option_keys

    if not isinstance(data, dict):
        raise _FlowHelperReadError(
            "Config subentry data must be an object", "invalid_data"
        )
    if sentinel_option_keys(data) or _contains_redacted_leaf(data):
        raise _FlowHelperReadError(
            "Config subentry data contains redacted values; capture is incomplete",
            "redacted_data",
        )
    return data


def _subentry_row(
    subentries: list[dict[str, Any]], entry_id: str, subentry_id: str
) -> dict[str, Any] | None:
    match = next((s for s in subentries if s.get("subentry_id") == subentry_id), None)
    if match is None:
        return None
    return {
        "entry_id": entry_id,
        "subentry_id": subentry_id,
        "subentry_type": match.get("subentry_type"),
        "title": match.get("title"),
        "data": _subentry_data(match.get("data")),
    }


class InvalidBackupSnapshotError(ValueError):
    """Persisted snapshot data is invalid, independently of live HA access."""


def _snapshot_validation_message(error: ValueError) -> str:
    """Keep local field-only validation messages, never raw YAML parser text."""
    return (
        str(error)
        if isinstance(error, InvalidBackupSnapshotError)
        else "Snapshot is invalid; inspect its YAML and schema version"
    )


class MandatoryBackupError(Exception):
    """A required pre-write snapshot could not be captured.

    Raised by ``maybe_snapshot(..., mandatory=True)`` when capture genuinely
    fails (fetch error, snapshot-write failure such as disk-full, or an
    unusable backup directory) — as opposed to a legitimate skip (nothing to
    snapshot for a new file/key). Deliberately a plain ``Exception`` and NOT a
    member of ``_CAPTURE_TRANSIENT_ERRORS`` so the ``@with_auto_backup``
    decorator's best-effort handler can't swallow it; the decorator maps it to
    a structured ``BACKUP_CAPTURE_FAILED`` error that fails the write closed.

    ``suggestions`` carries remediation surfaced in that structured error.
    """

    def __init__(
        self,
        message: str,
        *,
        suggestions: list[str] | None = None,
        safe_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []
        # Only locally authored diagnostics may opt in. Fetch/YAML exceptions
        # can include configuration values even when wrapped by this class.
        self.safe_detail = safe_detail

    @property
    def safe_cause(self) -> str:
        """The safe detail, else only the failing exception's class name."""
        return self.safe_detail or type(self.__cause__ or self).__name__


class BackupRestoreError(HomeAssistantError):
    """A restore outcome whose apply knowledge must survive caller mapping."""

    def __init__(
        self,
        message: str,
        *,
        apply_status: Literal["not_applied", "unknown", "applied"] = "not_applied",
        verification_status: Literal[
            "not_run", "matched", "mismatched", "unavailable"
        ] = "not_run",
        **outcome: Any,
    ) -> None:
        super().__init__(message)
        self.outcome = {
            **outcome,
            "apply_status": apply_status,
            "verification_status": verification_status,
        }
