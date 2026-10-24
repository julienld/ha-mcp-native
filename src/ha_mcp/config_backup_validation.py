"""Shared validation for backup settings without a server or transport."""

from typing import Any

from ha_mcp.config_registry import BACKUP_OVERRIDE_FIELDS, SETTING_BOUNDS


def _coerce_bool_field(field_name: str, raw: Any) -> tuple[Any, str | None]:
    """Coerce a posted value to bool, accepting the JSON/HTML-form spellings."""
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, int):
        return bool(raw), None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True, None
        if s in ("false", "0", "no", "off"):
            return False, None
        return None, f"Invalid boolean for {field_name}: {raw!r}"
    return None, f"Invalid value for {field_name}: {raw!r}"


def _coerce_int_field(field_name: str, raw: Any) -> tuple[Any, str | None]:
    """Coerce a posted value to a bounds-checked int (bools are rejected)."""
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return None, f"Invalid integer for {field_name}: {raw!r}"
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None, f"Invalid integer for {field_name}: {raw!r}"
    bounds = SETTING_BOUNDS.get(field_name)
    if bounds is not None and not (bounds[0] <= value <= bounds[1]):
        return None, f"{field_name} must be {bounds[0]:g}..{bounds[1]:g}"
    return value, None


def _coerce_str_field(field_name: str, raw: Any) -> tuple[Any, str | None]:
    """Coerce a posted value to a null-byte-free str."""
    if not isinstance(raw, str):
        return None, f"Invalid string for {field_name}: {raw!r}"
    if "\x00" in raw:
        return None, f"{field_name} must not contain null bytes"
    return raw, None


def _validate_backup_payload(payload: Any) -> tuple[dict[str, Any], str | None]:
    """Coerce and bounds-check the POST body. Returns (clean, error_msg)."""
    if not isinstance(payload, dict):
        return {}, "Body must be a JSON object"
    clean: dict[str, Any] = {}
    for field_name, _env_name, ftype in BACKUP_OVERRIDE_FIELDS:
        if field_name not in payload:
            continue
        raw = payload[field_name]
        if ftype is bool:
            value, err = _coerce_bool_field(field_name, raw)
        elif ftype is int:
            value, err = _coerce_int_field(field_name, raw)
        elif ftype is str:
            value, err = _coerce_str_field(field_name, raw)
        else:
            continue
        if err is not None:
            return {}, err
        clean[field_name] = value
    if not clean:
        return {}, "No editable auto-backup fields in body"
    return clean, None
