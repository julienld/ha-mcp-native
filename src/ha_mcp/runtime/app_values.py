"""Pure app option, identity and presentation rules shared with native owners."""

import re
from typing import Any

from ha_mcp.errors import create_validation_error

from .errors import raise_failure

_SUPERVISOR_SLUG_PATTERN = re.compile(r"[-_.A-Za-z0-9]+\Z")


def _merge_options(base: dict, override: dict) -> dict:
    """Merge caller options into current options with one-level deep merge.

    Top-level scalar values are replaced. Top-level dict values are merged
    one level deep so callers can update a single nested field (e.g.
    ``{"ssh": {"sftp": True}}``) without losing sibling fields.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _is_valid_supervisor_slug(value: str) -> bool:
    """Return whether a value is safe as one Supervisor path segment."""
    return (
        value not in {".", ".."}
        and _SUPERVISOR_SLUG_PATTERN.fullmatch(value) is not None
    )


def _validate_supervisor_slug(value: str, parameter: str = "slug") -> None:
    """Reject values that could escape a Supervisor path segment."""
    if _is_valid_supervisor_slug(value):
        return
    raise_failure(
        create_validation_error(
            f"{parameter!r} must be a valid Supervisor slug.",
            parameter=parameter,
            details=(
                "Use only ASCII letters, numbers, hyphens, underscores, and periods; "
                "the complete value cannot be '.' or '..'."
            ),
        )
    )


def _extract_addon_log_level(addon: dict[str, Any]) -> str | None:
    """Return the app (add-on)'s configured log level, if any.

    Checks the app's current options first (``options.log_level`` — what the
    user set), then falls back to the schema (Supervisor serializes ``schema``
    as a list of ``{name, type, ...}`` field descriptors) so apps that ship a
    log_level option without a value still surface ``"default"``. Returns
    ``None`` when the app exposes no log_level option at all.

    The lower-case ``"default"`` is the literal Supervisor sentinel; the
    integration path uses ``"DEFAULT"`` (uppercase) — these are distinct values
    by design and should not be cross-compared.
    """
    options = addon.get("options")
    if isinstance(options, dict):
        level = options.get("log_level")
        if isinstance(level, str) and level.strip():
            return level

    schema = addon.get("schema")
    if isinstance(schema, list) and any(
        isinstance(item, dict) and item.get("name") == "log_level" for item in schema
    ):
        return "default"

    return None


def _running_addon_slugs(addons: list[dict[str, Any]]) -> list[str]:
    """Return valid slugs for installed apps that Supervisor reports as running."""
    running_slugs: list[str] = []
    for addon in addons:
        if addon.get("state") != "started":
            continue
        slug = addon.get("slug")
        if not isinstance(slug, str) or not _is_valid_supervisor_slug(slug):
            raise TypeError("Supervisor returned a running app without a valid slug")
        running_slugs.append(slug)
    return running_slugs
