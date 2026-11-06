"""Project already-loaded HACS repositories without the mutating info handler."""

import copy
from typing import Any


def repository_data(owner: Any, repository: Any) -> dict[str, Any]:
    data: dict[str, Any] = copy.deepcopy(repository.data.to_json())
    data.update(
        name=repository.display_name,
        available_version=repository.display_available_version,
        installed_version=repository.display_installed_version,
        pending_upgrade=repository.pending_update,
        stars=repository.data.stargazers_count,
        releases=repository.data.published_tags,
        custom=not owner.repositories.is_default(str(repository.data.id)),
        can_download=repository.can_download,
        status=repository.display_status,
        state=repository.state,
        local_path=repository.content.path.local,
        country=repository.repository_manifest.country,
        homeassistant=repository.repository_manifest.homeassistant,
        hide_default_branch=repository.repository_manifest.hide_default_branch,
        ref=repository.ref,
        version_or_commit=repository.display_version_or_commit,
        additional_info=repository.additional_info,
        readme=repository.additional_info,
    )
    return data


def cache_status(owner: Any, repository: Any | None = None) -> dict[str, Any]:
    result = {
        "source": "hacs_cache",
        "hacs_version": str(owner.version),
        "refreshed_from_github": False,
        "hacs_disabled_reason": str(owner.system.disabled_reason)
        if owner.system.disabled_reason is not None
        else None,
    }
    if repository is not None:
        result.update(
            last_fetched=repository.data.last_fetched.isoformat()
            if repository.data.last_fetched
            else None,
            readme_available=bool(repository.additional_info),
            readme_source="hacs_cached_additional_info",
        )
    return result
