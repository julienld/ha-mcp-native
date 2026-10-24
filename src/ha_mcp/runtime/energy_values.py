"""Pure Energy conflict checks, duplicate rules and result projections."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.utils.config_hash import compute_config_hash

from .errors import raise_failure


def _flatten_validation_errors(raw: Any) -> list[dict[str, Any]]:
    """Add paths for readability while preserving native issue objects as details."""

    def walk(value: Any, path: str) -> list[dict[str, Any]]:
        if not value:
            return []
        if isinstance(value, list):
            return [
                issue
                for index, item in enumerate(value)
                for issue in walk(item, f"{path}[{index}]")
            ]
        if isinstance(value, dict) and not any(
            key in value for key in ("type", "message", "issue_type")
        ):
            return [
                issue
                for key, item in value.items()
                for issue in walk(item, f"{path}.{key}" if path else key)
            ]
        return [{"path": path, "message": str(value), "details": value}]

    return walk(raw, "")


def _check_config_hash(
    config: dict[str, Any],
    config_hash: str | dict[str, str],
    current_prefs: dict[str, Any],
) -> None:
    """Verify ``config_hash`` against ``current_prefs``; raises
    ``ToolFailure`` on mismatch or malformed per-key input.

    Extracted from ``_set_prefs`` step 2 (hash check). Handles both
    hash forms: a ``dict[str, str]`` (per-key lock) and a plain
    ``str`` (full-blob lock). See the ``_set_prefs`` docstring, and the
    ``ha_manage_energy_prefs`` tool docstring, for the full agent-facing
    contract this enforces.
    """
    if isinstance(config_hash, dict):
        submitted_keys = set(config)
        hashed_keys = set(config_hash)
        if not submitted_keys:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "'config' must include at least one top-level "
                    "key when using per-key config_hash",
                    context={"mode": "set"},
                    suggestions=[
                        "Include the top-level key(s) you want to "
                        "save in 'config' alongside their per-key "
                        "hashes in 'config_hash'",
                    ],
                )
            )
        if submitted_keys != hashed_keys:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Per-key config_hash keys must match the "
                    "top-level keys submitted in 'config'",
                    context={
                        "mode": "set",
                        "submitted_keys": sorted(submitted_keys),
                        "hashed_keys": sorted(hashed_keys),
                        "missing_in_hash": sorted(submitted_keys - hashed_keys),
                        "extra_in_hash": sorted(hashed_keys - submitted_keys),
                    },
                    suggestions=[
                        "Pass exactly one config_hash_per_key entry per top-level key in 'config'",
                        "Use the str form of config_hash to lock the full prefs blob instead",
                    ],
                )
            )

        mismatched_keys = [
            key
            for key in sorted(submitted_keys)
            if config_hash[key]
            != (
                compute_config_hash({key: current_prefs[key]})
                if key in current_prefs
                else None
            )
        ]
        if mismatched_keys:
            raise_failure(
                create_error_response(
                    ErrorCode.RESOURCE_LOCKED,
                    "Energy prefs modified since last read on "
                    f"top-level key(s): {', '.join(mismatched_keys)}"
                    " (conflict)",
                    context={
                        "mode": "set",
                        "mismatched_keys": mismatched_keys,
                    },
                    suggestions=[
                        "Call ha_manage_energy_prefs(mode='get') again",
                        "Re-apply your changes to the fresh config",
                        "Pass the new config_hash_per_key back in",
                    ],
                )
            )
    else:
        current_hash = compute_config_hash(current_prefs)
        if current_hash != config_hash:
            raise_failure(
                create_error_response(
                    ErrorCode.RESOURCE_LOCKED,
                    "Energy prefs modified since last read (conflict)",
                    context={"mode": "set"},
                    suggestions=[
                        "Call ha_manage_energy_prefs(mode='get') again",
                        "Re-apply your changes to the fresh config",
                        "Pass the new config_hash back in",
                    ],
                )
            )


def _append_unique_device(
    existing: list[dict[str, Any]],
    new_entry: dict[str, Any],
    target_key: str,
) -> list[dict[str, Any]]:
    """Append ``new_entry`` to ``existing`` if its ``stat_consumption`` is
    not already present. Raises ToolError(RESOURCE_ALREADY_EXISTS) on
    duplicate."""
    stat = new_entry["stat_consumption"]
    for entry in existing:
        if entry.get("stat_consumption") == stat:
            raise_failure(
                create_error_response(
                    ErrorCode.RESOURCE_ALREADY_EXISTS,
                    f"Device with stat_consumption='{stat}' already in {target_key}",
                    context={
                        "mode": "add_device",
                        "stat_consumption": stat,
                        "target_key": target_key,
                    },
                    suggestions=[
                        "Use mode='get' to inspect the current entries",
                        "Use mode='remove_device' first if you want to replace it",
                    ],
                )
            )
    return existing + [new_entry]


def _append_unique_source(
    existing: list[dict[str, Any]],
    new_source: dict[str, Any],
) -> list[dict[str, Any]]:
    """Append ``new_source`` to ``existing`` with type-aware duplicate
    detection.

    Solar/battery/gas/water entries are keyed on
    ``(type, stat_energy_from)`` — duplicates raise
    ``RESOURCE_ALREADY_EXISTS``. Grid entries are appended without a
    duplicate check (multiple grid variants are legitimate, and grid
    does not have a single canonical uniqueness key — see
    ``_add_source`` docstring for rationale). The post-save
    ``energy/validate`` call still runs on the full payload as a
    backstop for whatever HA Core flags.
    """
    source_type = new_source.get("type")
    if source_type != "grid" and "stat_energy_from" in new_source:
        stat = new_source.get("stat_energy_from")
        for entry in existing:
            if (
                entry.get("type") == source_type
                and entry.get("stat_energy_from") == stat
            ):
                raise_failure(
                    create_error_response(
                        ErrorCode.RESOURCE_ALREADY_EXISTS,
                        f"Source of type='{source_type}' with "
                        f"stat_energy_from='{stat}' already in energy_sources",
                        context={
                            "mode": "add_source",
                            "type": source_type,
                            "stat_energy_from": stat,
                            "target_key": "energy_sources",
                        },
                        suggestions=[
                            "Use mode='get' to inspect the current sources",
                            "Use mode='set' to replace the existing entry",
                        ],
                    )
                )
    return existing + [new_source]


def _remove_device_by_stat(
    existing: list[dict[str, Any]],
    stat_consumption: str,
    target_key: str,
) -> list[dict[str, Any]]:
    """Return ``existing`` minus the entry whose ``stat_consumption``
    matches. Raises ToolError(RESOURCE_NOT_FOUND) if no match."""
    kept = [e for e in existing if e.get("stat_consumption") != stat_consumption]
    if len(kept) == len(existing):
        raise_failure(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"No device with stat_consumption='{stat_consumption}' in {target_key}",
                context={
                    "mode": "remove_device",
                    "stat_consumption": stat_consumption,
                    "target_key": target_key,
                },
                suggestions=[
                    "Use mode='get' to inspect the current entries",
                    "Check water=True/False targets the right list",
                ],
            )
        )
    return kept


def _statistic_ids(value: Any) -> set[str]:
    """Collect statistic references across Core's source and device structures."""
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found.update(_statistic_ids(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if (
                (key.startswith("stat_") or key == "included_in_stat")
                and isinstance(item, str)
                and item
            ):
                found.add(item)
            elif isinstance(item, (dict, list)):
                found.update(_statistic_ids(item))
    return found


def compute_per_key_hashes(prefs: dict[str, Any]) -> dict[str, str]:
    """Hash every native preference slot without an allowlist or invented defaults."""
    return {key: compute_config_hash({key: value}) for key, value in prefs.items()}
