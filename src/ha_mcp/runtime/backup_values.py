"""Backup inventory projection and deletion safeguards shared across hosts."""

from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def _parse_backup_date(raw: Any) -> datetime | None:
    """Parse an HA backup `date` (ISO-8601, may use `Z` suffix) to a tz-aware
    datetime, returning None on missing/malformed input. Naive timestamps are
    treated as UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _backup_protected(entry: dict[str, Any]) -> bool | None:
    """Whether a ``backup/info`` entry is encrypted.

    ``protected`` is a per-agent field (AgentBackupStatus), not top-level. A
    backup is encrypted as a whole, so any agent reporting it makes the backup
    protected; returns None when the ``agents`` map is absent/malformed or no
    agent reports the flag (unknown rather than "unprotected").
    """
    agents = entry.get("agents")
    if not isinstance(agents, dict):
        return None
    flags = [
        a.get("protected")
        for a in agents.values()
        if isinstance(a, dict) and a.get("protected") is not None
    ]
    return any(flags) if flags else None


def _summarize_backup(entry: dict[str, Any]) -> dict[str, Any]:
    """Project one HA ``backup/info`` entry to the fields a caller needs to
    identify and choose a snapshot (issue #1586).

    Size is reported per backup agent; we surface the largest reported size
    across agents. Every field is ``.get`` so a schema the running HA version
    doesn't populate yields ``None`` rather than raising.
    """
    agents = entry.get("agents") or {}
    size_bytes: int | None = None
    if isinstance(agents, dict):
        sizes: list[int] = []
        for a in agents.values():
            if isinstance(a, dict):
                size = a.get("size")
                # Accept int or float (some agents report byte counts as float);
                # bool is an int subclass but never a real size, so exclude it.
                if isinstance(size, (int, float)) and not isinstance(size, bool):
                    sizes.append(int(size))
        if sizes:
            size_bytes = max(sizes)
    return {
        "backup_id": entry.get("backup_id"),
        "name": entry.get("name"),
        "date": entry.get("date"),
        "size_bytes": size_bytes,
        # per-agent field, derived from the agents map (see _backup_protected)
        "protected": _backup_protected(entry),
        "database_included": entry.get("database_included"),
        "homeassistant_included": entry.get("homeassistant_included"),
        "homeassistant_version": entry.get("homeassistant_version"),
        "with_automatic_settings": entry.get("with_automatic_settings"),
        "agent_ids": list(agents.keys()) if isinstance(agents, dict) else [],
    }


def _refuse_snapshot_delete(message: str, **context: Any) -> NoReturn:
    """Raise the shared VALIDATION_INVALID_PARAMETER shape for every
    snapshot-delete guard rejection (gate, confirm, automatic-settings, age
    floor, newest-snapshot protection)."""
    raise_failure(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            message,
            context=context,
        )
    )


def _newest_backup_id(backups: list[dict[str, Any]]) -> str | None:
    """Return the ``backup_id`` of the single most-recent entry by date, or
    None if no entry has a parseable date.

    Entries with a missing/malformed date are excluded from consideration —
    they can never be "the newest" — but that does not make them safe to
    delete; ``delete_backup`` fails closed on an unparseable target date
    separately, so an undated target can never be deleted regardless of
    this function's result.
    """
    dated = [
        (b.get("backup_id"), parsed)
        for b in backups
        if (parsed := _parse_backup_date(b.get("date"))) is not None
    ]
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[1])[0]


def _check_snapshot_deletable(
    matched: dict[str, Any],
    backups: list[dict[str, Any]],
    min_age_days: int,
    backup_id: str,
    now: datetime,
) -> None:
    """Apply the scheduled / newest / age-floor delete guards; raise to refuse."""
    if matched.get("with_automatic_settings") is not False:
        _refuse_snapshot_delete(
            "Refusing to delete a scheduled (automatic) backup, or one "
            "whose automatic/manual origin Home Assistant cannot "
            "confirm (with_automatic_settings=None — not created by "
            "this HA instance, or predates this metadata) — treated "
            "as not provably manual, and therefore not deletable.",
            backup_id=backup_id,
        )

    # Checked before the age floor below: a backup that is BOTH the
    # newest AND too young must report "newest" (create a new one
    # first), not "too young" — the age-floor message's remedy
    # ("lower snapshot_delete_min_age_days") would be actively
    # misleading advice for the newest snapshot, which stays
    # protected at any age floor including 0.
    newest_id = _newest_backup_id(backups)
    if newest_id == backup_id:
        _refuse_snapshot_delete(
            "Refusing to delete the newest snapshot — at least one "
            "recovery point must remain. Create a new snapshot first "
            "if you specifically need to free this one's space.",
            backup_id=backup_id,
        )

    target_date = _parse_backup_date(matched.get("date"))
    if target_date is None:
        _refuse_snapshot_delete(
            "Refusing to delete a backup with a missing or unparseable "
            "date — cannot verify it clears the minimum-age floor.",
            backup_id=backup_id,
        )
    cutoff = now - timedelta(days=min_age_days)
    # min_age_days=0 must unconditionally disable the floor, even if
    # target_date is slightly ahead of this host's clock (clock skew
    # vs the HA instance that stamped it) — guard explicitly rather
    # than relying on the arithmetic reduction, which clock skew breaks.
    if min_age_days > 0 and target_date > cutoff:
        _refuse_snapshot_delete(
            f"Refusing to delete a backup younger than "
            f"snapshot_delete_min_age_days={min_age_days} days — at "
            "least one recent recovery point must remain. Delete an "
            "older backup, or lower snapshot_delete_min_age_days if "
            "this recurs.",
            backup_id=backup_id,
        )
