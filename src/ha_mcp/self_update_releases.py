"""Conservative release assessment, explicitly distinct from installability."""

import re
from collections.abc import Iterable
from typing import Any

from packaging.version import InvalidVersion, Version


def _release_catalog(releases: Iterable[Any]) -> dict[Version, Any]:
    catalog = {}
    for release in releases:
        try:
            version = Version(release.tag_name)
        except (InvalidVersion, AttributeError):
            continue
        if not version.is_prerelease:
            catalog[version] = getattr(release, "body", None)
    return catalog


def _declares_breaking(notes: str) -> bool:
    notes = re.sub(
        r"\bno breaking changes?\b|\bnot a breaking change\b", "", notes, flags=re.I
    )
    return bool(
        re.search(
            r"\bbreaking[\s_:-]+changes?\b|\bbackwards? incompatible\b|"
            r"\b(?:connection URL|authentication|runtime|distribution|data format)\b[^\n.]{0,80}\b(?:changed|required migration|must migrate|removed)\b|"
            r"\b(?:must migrate|migration required)\b",
            notes,
            re.I,
        )
    )


def assess_releases(
    installed: str,
    offered: str,
    releases: Iterable[Any],
    *,
    detected_changes: Iterable[str] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "classification": "unknown",
        "versions_checked": [],
        "reason": "A complete stable release interval is unavailable.",
        "basis": "Installed owner's release catalog; semantic version and declared breaking-change markers. This is not a guarantee of compatibility.",
    }
    changes = sorted(set(detected_changes))
    if changes:
        return {
            **result,
            "classification": "breaking",
            "reason": "Detected installation-contract changes require review.",
            "changes": changes,
        }
    try:
        before, after = Version(installed), Version(offered)
    except InvalidVersion:
        return result
    if before == after:
        return {
            **result,
            "classification": "unchanged",
            "reason": "The offered version is already installed.",
        }
    if after < before or before.major != after.major:
        return {
            **result,
            "classification": "breaking",
            "reason": "Major-version change or downgrade requires review.",
        }
    if before.is_prerelease or after.is_prerelease or before.local or after.local:
        return result
    catalog = _release_catalog(releases)
    if before not in catalog or after not in catalog:
        return result
    interval = sorted(version for version in catalog if before < version <= after)
    if any(
        not isinstance(catalog[version], str) or not catalog[version].strip()
        for version in interval
    ):
        return result
    result["versions_checked"] = [str(version) for version in interval]
    breaking = any(_declares_breaking(catalog[version]) for version in interval)
    return {
        **result,
        "classification": "breaking" if breaking else "compatible",
        "reason": "Release notes declare breaking changes."
        if breaking
        else "Same stable major; covered release notes declare no breaking changes.",
    }
