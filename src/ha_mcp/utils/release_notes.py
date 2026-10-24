"""Best-effort public release notes shared by native and standalone hosts."""

import asyncio
import logging
import re
from typing import Any

import httpx

from ha_mcp.runtime.update_values import (
    _extract_blog_content,
    _get_monthly_versions_between,
    _parse_breaking_changes_html,
    _parse_patch_breaking_changes,
)

logger = logging.getLogger(__name__)
_GITHUB_CORE_RELEASE_URL = (
    "https://api.github.com/repos/home-assistant/core/releases/tags/{version}"
)


async def _fetch_release_data_for_version(
    http_client: httpx.AsyncClient, version: str
) -> dict[str, Any] | None:
    """Fetch release notes and breaking changes for a single HA Core version."""
    try:
        resp = await http_client.get(_GITHUB_CORE_RELEASE_URL.format(version=version))
        if resp.status_code != 200:
            return None
        body = resp.json().get("body", "").strip()

        if body.startswith("https://www.home-assistant.io/blog/"):
            blog_resp = await http_client.get(body)
            if blog_resp.status_code == 200:
                bc = _parse_breaking_changes_html(blog_resp.text, body)
                return {
                    "entries": bc["entries"] if bc else [],
                    "count": bc["count"] if bc else 0,
                    "source_url": body,
                    "release_notes": _extract_blog_content(blog_resp.text),
                }

        if "(breaking-change)" in body.lower():
            return _parse_patch_breaking_changes(body, version)
        return None
    except (httpx.RequestError, ValueError, KeyError) as e:
        logger.debug(f"Failed to fetch release data for {version}: {e}")
        return None


async def _fetch_release_data(
    current_version: str, target_version: str
) -> dict[str, Any]:
    """Fetch release notes and breaking changes for all monthly versions in range."""
    monthly = _get_monthly_versions_between(current_version, target_version)
    if not monthly:
        return {"entries": [], "count": 0, "versions_checked": [], "release_notes": []}

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={
            "User-Agent": "HomeAssistant-MCP-Server",
            "Accept": "application/vnd.github+json",
        },
    ) as http_client:
        results = await asyncio.gather(
            *[_fetch_release_data_for_version(http_client, v) for v in monthly],
            return_exceptions=True,
        )

    all_entries: list[dict[str, Any]] = []
    versions_checked: list[str] = []
    release_notes: list[dict[str, str]] = []

    for version, result in zip(monthly, results, strict=True):
        if not isinstance(result, dict):
            continue
        versions_checked.append(version)
        src = result.get("source_url", "")
        all_entries.extend(
            {**entry, "version": version} for entry in result.get("entries", [])
        )
        notes = result.get("release_notes", "")
        if notes:
            release_notes.append(
                {"version": version, "content": notes, "source_url": src}
            )

    return {
        "entries": all_entries,
        "count": len(all_entries),
        "versions_checked": versions_checked,
        "release_notes": release_notes,
    }


async def _try_raw_cdn(
    http_client: httpx.AsyncClient, owner: str, repo: str, tag: str
) -> dict[str, str] | None:
    raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{tag}"
    changelog_paths = [
        "CHANGELOG.md",
        "RELEASES.md",
        "RELEASE_NOTES.md",
        f"docs/releases/{tag}.md",
        "docs/CHANGELOG.md",
    ]
    for path in changelog_paths:
        raw_url = f"{raw_base}/{path}"
        try:
            response = await http_client.get(
                raw_url,
                headers={"User-Agent": "HomeAssistant-MCP-Server"},
            )
            if response.status_code == 200:
                content = response.text
                if content and len(content) > 50:
                    logger.debug(
                        f"Successfully fetched release notes from raw CDN: {raw_url}"
                    )
                    return {"notes": content, "source": "github_raw"}
        except httpx.RequestError as raw_error:
            logger.debug(f"Failed to fetch from {raw_url}: {raw_error}")
    return None


async def _fetch_github_release_notes(release_url: str) -> dict[str, str] | None:
    """
    Fetch release notes from GitHub releases API with fallback to raw CDN.

    Tries multiple sources in order:
    1. GitHub API (best formatting, but rate limited)
    2. GitHub raw content CDN (no rate limits, but may not have release notes)

    Parses GitHub release URLs and fetches the release body from the API.

    Args:
        release_url: URL to a GitHub release page

    Returns:
        Dictionary with 'notes' and 'source' keys, or None if fetch fails
    """
    try:
        # Parse GitHub URL patterns:
        # https://github.com/owner/repo/releases/tag/v1.2.3
        # https://github.com/owner/repo/releases/v1.2.3

        github_pattern = (
            r"https://github\.com/([^/]+)/([^/]+)/releases(?:/tag)?/([^/?#]+)"
        )
        match = re.match(github_pattern, release_url)

        if not match:
            logger.debug(f"Could not parse GitHub URL: {release_url}")
            return None

        owner, repo, tag = match.groups()

        async with httpx.AsyncClient(timeout=15.0) as http_client:
            # Try 1: GitHub API (has release notes in structured format)
            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"

            response = await http_client.get(
                api_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "HomeAssistant-MCP-Server",
                },
            )

            if response.status_code == 200:
                release_data = response.json()
                body = release_data.get("body", "")
                if body:
                    return {"notes": str(body), "source": "github_api"}
            elif response.status_code == 403:
                # Check if rate limited
                remaining = response.headers.get("X-RateLimit-Remaining", "0")
                if remaining == "0":
                    logger.warning(
                        f"GitHub API rate limit exceeded for {api_url}, trying raw CDN fallback"
                    )
            else:
                logger.debug(
                    f"GitHub API returned status {response.status_code} for {api_url}"
                )

            # Try 2: GitHub raw content CDN (for markdown files)
            cdn_result = await _try_raw_cdn(http_client, owner, repo, tag)
            if cdn_result:
                return cdn_result

            logger.debug(
                f"Could not fetch release notes from API or raw CDN for {release_url}"
            )
            return None

    except Exception as e:  # noqa: BLE001
        logger.debug(f"Failed to fetch GitHub release notes: {e}")
        return None


async def _fetch_core_release_notes(version: str) -> dict[str, str] | None:
    """
    Fetch release notes for Home Assistant Core from GitHub releases API.

    Home Assistant Core uses blog URLs for release_url which don't contain
    the actual release notes. This function fetches directly from GitHub
    releases using the version tag.

    Args:
        version: The version string (e.g., "2025.11.3")

    Returns:
        Dictionary with 'notes' and 'source' keys, or None if fetch fails
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            # GitHub API URL for Home Assistant Core releases
            api_url = f"https://api.github.com/repos/home-assistant/core/releases/tags/{version}"

            response = await http_client.get(
                api_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "HomeAssistant-MCP-Server",
                },
            )

            if response.status_code == 200:
                release_data = response.json()
                body = release_data.get("body", "")
                if body:
                    logger.debug(
                        f"Successfully fetched Core release notes from GitHub for version {version}"
                    )
                    return {"notes": str(body), "source": "github_api"}
            elif response.status_code == 403:
                # Check if rate limited
                remaining = response.headers.get("X-RateLimit-Remaining", "0")
                if remaining == "0":
                    logger.warning(f"GitHub API rate limit exceeded for {api_url}")
            else:
                logger.debug(
                    f"GitHub API returned status {response.status_code} for Core release {version}"
                )

            return None

    except Exception as e:  # noqa: BLE001
        logger.debug(f"Failed to fetch Core release notes from GitHub: {e}")
        return None
