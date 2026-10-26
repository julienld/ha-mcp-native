"""App results preserve existing presentation without owning a connection."""

from typing import Any

from .app_values import _extract_addon_log_level
from .redaction import redact_addon_info


def app_info_result(
    addon: dict[str, Any], slug: str, *, redact: bool, option_keys: str = ""
) -> dict[str, Any]:
    result: dict[str, Any] = {"success": True, "addon": addon}

    if redact:
        result["addon"], warnings = redact_addon_info(
            addon, slug, option_keys=option_keys
        )
        if warnings:
            result["warnings"] = warnings

    # Extracted AFTER redaction so an app (add-on) that schema-marks log_level as
    # a password surfaces the sentinel here, never the live value.
    log_level = _extract_addon_log_level(result["addon"])
    if log_level is not None:
        result["log_level"] = log_level

    return result


def installed_apps_result(
    addons: list[dict[str, Any]],
    include_stats: bool,
    stats_by_slug: dict[str, Any],
    stats_warnings: list[str],
    *,
    preserve_native: bool = False,
) -> dict[str, Any]:
    # Format app information
    formatted_addons = []
    for addon in addons:
        addon_info = {
            **(addon if preserve_native else {}),
            "name": addon.get("name"),
            "slug": addon.get("slug"),
            "description": addon.get("description"),
            "version": addon.get("version"),
            "installed": True,
            "state": addon.get("state"),
            "update_available": addon.get("update_available", False),
            "repository": addon.get("repository"),
        }

        if include_stats:
            slug = addon.get("slug")
            addon_info["stats"] = (
                stats_by_slug.get(slug) if isinstance(slug, str) else None
            )

        formatted_addons.append(addon_info)

    # Count apps by state
    running_count = sum(1 for a in addons if a.get("state") == "started")
    update_count = sum(1 for a in addons if a.get("update_available"))

    result: dict[str, Any] = {
        "success": True,
        "addons": formatted_addons,
        "summary": {
            "total_installed": len(formatted_addons),
            "running": running_count,
            "stopped": len(formatted_addons) - running_count,
            "updates_available": update_count,
        },
    }
    if stats_warnings:
        result["warnings"] = stats_warnings
    return result


def available_apps_result(
    data: dict[str, Any],
    repository: str | None,
    query: str | None,
    *,
    preserve_native: bool = False,
) -> dict[str, Any]:
    repositories = data.get("repositories", [])
    addons = data.get("addons", [])

    # Format repository information
    formatted_repos = [
        {
            **(repo if preserve_native else {}),
            "slug": repo.get("slug"),
            "name": repo.get("name"),
            "source": repo.get("source"),
            "maintainer": repo.get("maintainer"),
        }
        for repo in repositories
    ]

    # Filter and format apps
    formatted_addons = []
    for addon in addons:
        # Apply repository filter
        if repository and addon.get("repository") != repository:
            continue

        # Apply search query filter
        if query:
            query_lower = query.lower()
            name = (addon.get("name") or "").lower()
            description = (addon.get("description") or "").lower()
            if query_lower not in name and query_lower not in description:
                continue

        addon_info = {
            **(addon if preserve_native else {}),
            "name": addon.get("name"),
            "slug": addon.get("slug"),
            "description": addon.get("description"),
            # A /store entry's `version` is the INSTALLED version and is None
            # for anything not installed; `version_latest` is what you would
            # get by installing it (supervisor/api/store.py). A store listing
            # means the latter, so report that as `version` and keep the
            # installed one alongside it.
            "version": addon.get("version_latest"),
            "version_installed": addon.get("version"),
            "available": addon.get("available", True),
            "installed": addon.get("installed", False),
            "repository": addon.get("repository"),
            "url": addon.get("url"),
            "icon": addon.get("icon"),
            "logo": addon.get("logo"),
        }
        formatted_addons.append(addon_info)

    # Count statistics
    installed_count = sum(1 for a in formatted_addons if a.get("installed"))

    return {
        "success": True,
        "repositories": formatted_repos,
        "addons": formatted_addons,
        "summary": {
            "total_available": len(formatted_addons),
            "installed": installed_count,
            "not_installed": len(formatted_addons) - installed_count,
            "repository_count": len(formatted_repos),
        },
        "filters_applied": {
            "repository": repository,
            "query": query,
        },
    }
