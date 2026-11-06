"""Store refresh and app configuration results shared across hosts."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def store_versions(result: Any) -> dict[str, dict[str, Any]]:
    addons = result.get("addons") if isinstance(result, dict) else None
    # A partial snapshot is worse than no snapshot: the diff walks both
    # sides, so every slug missing from a truncated reading comes back as
    # an app that left the store. Refuse the whole reading instead, which
    # degrades to None and skips the comparison with a warning.
    if not isinstance(addons, list):
        raise_failure(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Supervisor's /store response carried no readable app (add-on) list.",
                details=(
                    f"expected a list at result.addons, got {type(addons).__name__}"
                ),
            )
        )
    snapshot: dict[str, dict[str, Any]] = {}
    for addon in addons:
        slug = addon.get("slug") if isinstance(addon, dict) else None
        if not isinstance(slug, str) or not slug:
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Supervisor's /store response carried an app (add-on) "
                    "entry with no usable slug.",
                    details=(
                        "every entry needs a non-empty string slug to be "
                        "comparable across a reload"
                    ),
                )
            )
        snapshot[slug] = {
            "name": addon.get("name"),
            "version": addon.get("version_latest"),
            "update_available": bool(addon.get("update_available", False)),
        }
    return snapshot


def diff_store_versions(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the apps (add-ons) whose latest-known version moved.

    Both directions of appearance are changes a caller reloads the store
    to find, so both are reported and the null side says which it is:
    ``version_before: None`` is newly discovered, ``version_after: None``
    is gone from the store (its repository was removed or is unreachable).
    That only holds when each snapshot is a real reading of the store,
    which is why an unreadable one is ``None`` rather than ``{}`` and
    skips the diff entirely.
    """
    changed: list[dict[str, Any]] = []
    for slug in sorted(before.keys() | after.keys()):
        previous = before.get(slug)
        current = after.get(slug)
        if (
            previous is not None
            and current is not None
            and previous.get("version") == current.get("version")
        ):
            continue
        known = current if current is not None else previous
        changed.append(
            {
                "slug": slug,
                "name": (known or {}).get("name"),
                "version_before": (previous or {}).get("version"),
                "version_after": (current or {}).get("version"),
            }
        )
    return changed


def store_refresh_result(
    before: dict[str, dict[str, Any]] | None,
    after: dict[str, dict[str, Any]] | None,
    warnings: list[str],
) -> dict[str, Any]:
    # Only a pair of real readings can be compared, and only a real
    # `after` reading can list updates. An unmeasured count reported as 0
    # reads as "looked and found nothing", so each unmeasured field stays
    # None and the message says it is unknown rather than zero.
    changed: list[dict[str, Any]] | None = None
    if before is None or after is None:
        warnings.append(
            "Skipped the changed-app comparison because the store could "
            "not be read on both sides of the reload."
        )
    else:
        changed = diff_store_versions(before, after)
    updates_available: list[dict[str, Any]] | None = None
    if after is None:
        warnings.append(
            "Could not list available updates because the store could not "
            "be read after the reload."
        )
    else:
        updates_available = [
            {
                "slug": slug,
                "name": entry.get("name"),
                "version": entry.get("version"),
            }
            for slug, entry in sorted(after.items())
            if entry.get("update_available")
        ]
    changed_text = (
        "changed apps unknown"
        if changed is None
        else f"{len(changed)} app version(s) changed"
    )
    updates_text = (
        "available updates unknown"
        if updates_available is None
        else f"{len(updates_available)} update(s) available"
    )
    result: dict[str, Any] = {
        "success": True,
        "action": "check_updates",
        "changed": changed,
        "updates_available": updates_available,
        "message": (
            f"App (add-on) store reloaded; {changed_text}, {updates_text}. "
            "This refreshes store metadata only — install a new version "
            "with action='update', or action='rebuild' for a local app "
            "whose source changed but whose version did not."
        ),
    }
    if warnings:
        result["warnings"] = warnings
    return result


def config_submitted_result(
    slug: str, config_data: dict[str, Any], ignored_fields: list[str]
) -> dict[str, Any]:
    submitted_fields = list(config_data.keys())
    if {"options", "network"} & config_data.keys():
        response: dict = {
            "status": "pending_restart",
            "message": (
                f"Configuration submitted for app (add-on) '{slug}'. "
                "Restart the app for options/network changes to take effect."
            ),
            "submitted_fields": submitted_fields,
        }
    else:
        response = {
            "success": True,
            "message": f"Configuration updated for app (add-on) '{slug}'.",
            "submitted_fields": submitted_fields,
        }
    if ignored_fields:
        response.setdefault("warnings", []).append(
            f"{len(ignored_fields)} field(s) not in app (add-on) schema were ignored "
            f"before write: {ignored_fields}. Use ha_get_app(slug) to see the "
            "declared schema."
        )
        response["ignored_fields"] = ignored_fields
    return response
