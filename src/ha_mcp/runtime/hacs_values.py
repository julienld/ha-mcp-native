"""HACS category presentation, scoring and input policies without a client."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

# HACS uses different category names internally vs what users expect
# User-friendly name -> HACS internal name
CATEGORY_MAP = {
    "lovelace": "plugin",  # HACS calls Lovelace cards "plugin"
    "integration": "integration",
    "theme": "theme",
    "appdaemon": "appdaemon",
    "python_script": "python_script",
    "template": "template",
}

# Reverse mapping for display
CATEGORY_DISPLAY = {v: k for k, v in CATEGORY_MAP.items()}
CATEGORY_DISPLAY["plugin"] = "lovelace"  # Display as lovelace for users


def _score_repo_against_query(
    query_lower: str, name: str, full_name: str, description: str, authors: str
) -> int:
    """Compute a relevance score for a repo's text fields against a query."""
    score = 0
    if query_lower in name:
        score += 100
    if query_lower in full_name:
        score += 50
    if query_lower in description:
        score += 30
    if query_lower in authors:
        score += 20
    return score


def _filter_and_score_repos(
    all_repositories: list[dict[str, Any]],
    query: str,
    installed_only: bool | None,
) -> list[dict[str, Any]]:
    """Filter repositories and compute relevance scores."""
    query_lower = query.lower().strip()
    matches = []

    for repo in all_repositories:
        if installed_only and not repo.get("installed", False):
            continue

        # Handle None values safely
        name = (repo.get("name") or "").lower()
        description = (repo.get("description") or "").lower()
        full_name = (repo.get("full_name") or "").lower()
        authors_list = repo.get("authors") or []
        authors = " ".join(authors_list).lower()

        # Calculate relevance score (all repos match when query is empty)
        if query_lower:
            score = _score_repo_against_query(
                query_lower, name, full_name, description, authors
            )
            if score == 0:
                continue
        else:
            score = 0

        # Map HACS internal category back to user-friendly name
        repo_category = repo.get("category", "")
        display_category = CATEGORY_DISPLAY.get(repo_category, repo_category)
        entry: dict[str, Any] = {
            "name": repo.get("name"),
            "full_name": repo.get("full_name"),
            "description": repo.get("description"),
            "category": display_category,
            "id": repo.get("id"),
            "stars": repo.get("stars", 0),
            "downloads": repo.get("downloads", 0),
            "authors": authors_list,
            "installed": repo.get("installed", False),
            "installed_version": repo.get("installed_version")
            if repo.get("installed")
            else None,
            "available_version": repo.get("available_version"),
        }
        if query_lower:
            entry["score"] = score
        if repo.get("installed"):
            entry["pending_update"] = repo.get("pending_upgrade", False)
            entry["domain"] = repo.get("domain")
        matches.append(entry)

    # Sort by score (descending) when searching, by name when listing
    if query_lower:
        matches.sort(key=lambda x: x.get("score", 0), reverse=True)
    else:
        matches.sort(key=lambda x: (x.get("name") or "").lower())

    return matches


def _reject_foreign_params(action: str, **per_action: dict[str, Any]) -> None:
    """Raise when a parameter belonging to a different action was supplied.

    ``per_action`` maps each action name to the {param_name: value} pairs
    that are FOREIGN to it; a non-None value among them is a caller mixing
    action vocabularies (e.g. ``version`` with ``action="remove"``), which
    silently ignoring would let a call do something other than what its
    arguments describe.
    """
    foreign = {
        name: value
        for name, value in per_action.get(action, {}).items()
        if value is not None
    }
    if foreign:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Parameter(s) {sorted(foreign)} do not apply to "
                f"action='{action}' and would be ignored — remove them or "
                "switch action.",
                context={"action": action, "foreign_parameters": sorted(foreign)},
            )
        )
