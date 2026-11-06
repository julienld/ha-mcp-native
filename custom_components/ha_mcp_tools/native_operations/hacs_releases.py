"""Explicit release reads using the pinned store and Core update owners.

Source contracts: HACS 2.0.5 websocket/repository.py and Core 2026.11.0b2
marketplace/websocket/repository.py and marketplace/update.py. No info handler
is invoked: it refreshes metadata and clears the repository's new flag.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Any

from ha_mcp.tool_api import ToolFailure, UnsupportedOperation

from . import hacs_updates
from .hacs_access import HacsAccess

logger = logging.getLogger(__name__)


async def _read(
    access: HacsAccess,
    repository: Any,
    before: dict[str, Any],
    fetch: Callable[[], Awaitable[Any]],
    label: str,
) -> tuple[Any, list[str]]:
    await access.observe(repository, before)
    value, warnings = None, []
    try:
        async with asyncio.timeout(60):
            value = await fetch()
    except ToolFailure:
        raise
    except Exception as error:
        logger.debug("%s read failed", label, exc_info=True)
        warnings.append(f"{label} unavailable: {error}")
    # Provider failure may coincide with actor revocation or owner replacement;
    # neither permits returning the earlier cached configuration.
    await access.observe(repository, before)
    return value, warnings


async def _releases(
    access: HacsAccess,
    repository: Any,
    before: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    command = f"{access.domain}/repository/releases"
    try:
        _payload, registration = access.validate(
            command, {"repository_id": str(repository.data.id)}
        )
    except UnsupportedOperation as error:
        return None, [f"Releases unavailable: {error}"]
    getter = repository.async_get_releases

    async def fetch() -> Any:
        access.pin_validator(command, registration)
        if repository.async_get_releases != getter:
            raise ToolFailure(
                "CONFIG_CONFLICT", "Store release owner changed", status="not_started"
            )
        return await getter()

    rows, warnings = await _read(access, repository, before, fetch, "Releases")
    access.pin_validator(command, registration)
    return (
        [
            {
                "name": release.name,
                "tag": release.tag_name,
                "published_at": release.published_at,
                "prerelease": release.prerelease,
            }
            for release in rows
        ]
        if rows is not None
        else None,
        warnings,
    )


async def _notes(
    access: HacsAccess,
    repository: Any,
    before: dict[str, Any],
) -> tuple[Any, list[str]]:
    if access.domain == "hacs":
        command = "hacs/repository/release_notes"
        _payload, registration = access.validate(
            command, {"repository": str(repository.data.id)}
        )
        await access.observe(repository, before)
        access.pin_validator(command, registration)
        newer = import_module(
            "custom_components.hacs.utils.version"
        ).version_left_higher_then_right
        return [
            {"name": release.name, "tag": release.tag_name, "body": release.body}
            for release in repository.releases.objects
            if not repository.data.installed_version
            or newer(release.tag_name, repository.data.installed_version)
        ], []
    if not repository.pending_update:
        return None, []
    target = hacs_updates.repository_update_target(access, repository)
    if target is None:
        return None, []
    getter = target.entity.async_release_notes

    async def fetch() -> Any:
        hacs_updates.check_update_target(access.hass, target)
        current = hacs_updates.repository_update_target(access, repository)
        if current is None or current.entity is not target.entity:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Core release-notes target changed",
                status="not_started",
            )
        if target.entity.async_release_notes != getter:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Core release-notes owner changed",
                status="not_started",
            )
        return await getter()

    result = await _read(access, repository, before, fetch, "Release notes")
    hacs_updates.check_update_target(access.hass, target)
    current = hacs_updates.repository_update_target(access, repository)
    if current is None or current.entity is not target.entity:
        raise ToolFailure(
            "CONFIG_CONFLICT", "Core release-notes target changed", status="not_started"
        )
    return result


async def release_details(
    access: HacsAccess,
    repository: Any,
    before: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    releases, warnings = await _releases(access, repository, before)
    if releases is not None:
        result["github_releases"] = releases
    try:
        notes, note_warnings = await _notes(access, repository, before)
    except UnsupportedOperation as error:
        notes, note_warnings = None, [f"Release notes unavailable: {error}"]
    except ToolFailure:
        raise
    except (AttributeError, ImportError) as error:
        notes, note_warnings = None, [f"Release notes unavailable: {error}"]
    warnings.extend(note_warnings)
    if notes is not None:
        result["release_notes"] = notes
    if warnings:
        result["warnings"] = warnings
    await access.observe(repository, before)
    return result
