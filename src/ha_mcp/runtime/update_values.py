"""Pure update presentation and release-note parsing shared by both hosts."""

import re
from typing import Any

_MAX_MONTHLY_VERSIONS = 12


def _parse_version(version_str: str) -> tuple[int, ...] | None:
    """Parse '2025.11.3' into a comparable tuple, or None on failure."""
    if not version_str:
        return None
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return None


def _get_monthly_versions_between(current: str, target: str) -> list[str]:
    """Return .0 monthly versions between current (exclusive) and target (inclusive)."""
    current_parts = _parse_version(current)
    target_parts = _parse_version(target)
    if (
        not current_parts
        or not target_parts
        or len(current_parts) < 2
        or len(target_parts) < 2
    ):
        if target_parts and len(target_parts) >= 2:
            return [f"{target_parts[0]}.{target_parts[1]}.0"]
        return []

    versions: list[str] = []
    year, month = current_parts[0], current_parts[1] + 1
    while (year, month) <= (target_parts[0], target_parts[1]):
        versions.append(f"{year}.{month}.0")
        month += 1
        if month > 12:
            month, year = 1, year + 1
        if len(versions) >= _MAX_MONTHLY_VERSIONS:
            break
    return versions


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace for readable plain text."""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<p[^>]*>", "\n", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_blog_content(html: str) -> str:
    """Extract article body from an HA blog post as plain text."""
    article = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
    if article:
        return _strip_html(article.group(1))
    content = re.search(
        r"(<h[12][^>]*>.*?)(?=<div[^>]*id=\"discourse|<footer[^>]*>|</body>)",
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if content:
        return _strip_html(content.group(1))
    return _strip_html(html)


def _parse_breaking_changes_html(html: str, source_url: str) -> dict[str, Any] | None:
    """Extract 'Backward-incompatible changes' section entries from blog HTML."""
    section_match = re.search(
        r'id="backward-incompatible-changes"[^>]*>.*?</h2>(.*?)(?=<h2[ >]|</article>|</main>)',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return None

    entries: list[dict[str, str]] = []
    for match in re.finditer(
        r"<h3[^>]*>(.*?)</h3>(.*?)(?=<h3[^>]*>|$)", section_match.group(1), re.DOTALL
    ):
        name = _strip_html(match.group(1)).strip()
        desc = _strip_html(match.group(2)).strip()
        if name:
            entries.append({"integration": name, "description": desc})

    if not entries:
        return None
    return {"entries": entries, "count": len(entries), "source_url": source_url}


def _parse_patch_breaking_changes(body: str, version: str) -> dict[str, Any] | None:
    """Parse (breaking-change) tagged items from a GitHub patch-release body."""
    entries: list[dict[str, str]] = []
    for line in body.split("\n"):
        if "(breaking-change)" not in line.lower():
            continue
        clean = (
            re.sub(r"\(breaking-change\)", "", line, flags=re.IGNORECASE)
            .lstrip("-*")
            .strip()
        )
        if not clean:
            continue
        doc_match = re.search(
            r"\[([^\]]+?)\s+(?:docs|documentation)\]", clean, re.IGNORECASE
        )
        integration = doc_match.group(1).strip() if doc_match else "unknown"
        entries.append({"integration": integration, "description": clean})

    if not entries:
        return None
    return {
        "entries": entries,
        "count": len(entries),
        "source_url": f"https://github.com/home-assistant/core/releases/tag/{version}",
    }


def _supports_release_notes(entity_id: str, attributes: dict[str, Any]) -> bool:
    """
    Determine if an update entity supports fetching release notes.

    Returns True if the entity supports release notes through any method:
    - WebSocket update/release_notes command (native HA support)
    - GitHub API/raw CDN fallback (when release_url is available)

    Most entities will return True as they have either native support or a release_url.
    """
    # Check for supported_features that indicate release notes support
    # Feature flag 1 = install, 2 = specific_version, 4 = progress, 8 = backup
    # 16 = release_notes (0x10)
    supported_features = attributes.get("supported_features", 0)
    has_release_notes_feature = (supported_features & 16) != 0

    # Entity supports release notes if it has either:
    # 1. Native WebSocket support (feature flag)
    # 2. A release_url (can fetch from GitHub)
    return has_release_notes_feature or attributes.get("release_url") is not None


def _categorize_update(entity_id: str, attributes: dict[str, Any]) -> str:
    """Categorize an update entity based on its entity_id and attributes."""
    entity_lower = entity_id.lower()
    # Use 'or ""' to handle both missing keys AND explicit None values
    title_lower = (attributes.get("title") or "").lower()

    # Core update
    if "home_assistant_core" in entity_lower or (
        "core" in entity_lower and "home_assistant" in title_lower
    ):
        return "core"

    # Operating System
    if "operating_system" in entity_lower or "haos" in entity_lower:
        return "os"

    # Supervisor
    if "supervisor" in entity_lower:
        return "supervisor"

    # HACS updates
    if "hacs" in entity_lower:
        return "hacs"

    # Add-ons (typically named update.xxx_update where xxx is addon name)
    # Add-ons usually have "Add-on" in title or specific patterns
    if "add-on" in title_lower or "addon" in title_lower:
        return "addons"

    # Device firmware updates (ESPHome, Z-Wave, Zigbee, etc.)
    device_patterns = ["esphome", "zwave", "zigbee", "zha", "matter", "firmware"]
    if any(
        pattern in entity_lower or pattern in title_lower for pattern in device_patterns
    ):
        return "devices"

    # Default to other
    return "other"


def update_listing(
    states: list[dict[str, Any]],
    include_skipped: bool,
    *,
    include_attributes: bool = False,
    native_categories: dict[str, str] | None = None,
) -> dict[str, Any]:
    update_entities = [
        s for s in states if s.get("entity_id", "").startswith("update.")
    ]

    available_updates = []
    skipped_updates = []

    for entity in update_entities:
        entity_id = entity.get("entity_id", "")
        state = entity.get("state", "")
        attributes = entity.get("attributes", {})

        is_available = state == "on"
        is_skipped = attributes.get("skipped_version") is not None

        update_info = {
            "entity_id": entity_id,
            "title": attributes.get("title", entity_id),
            "installed_version": attributes.get("installed_version"),
            "latest_version": attributes.get("latest_version"),
            "release_summary": attributes.get("release_summary"),
            "release_url": attributes.get("release_url"),
            "can_install": not attributes.get("in_progress", False),
            "in_progress": attributes.get("in_progress", False),
            "supports_release_notes": _supports_release_notes(entity_id, attributes),
            "skipped_version": attributes.get("skipped_version"),
            "auto_update": attributes.get("auto_update", False),
            "category": (native_categories or {}).get(entity_id)
            or _categorize_update(entity_id, attributes),
        }
        if include_attributes:
            update_info["attributes"] = attributes

        if is_skipped:
            skipped_updates.append(update_info)
        elif is_available:
            available_updates.append(update_info)

    all_updates = available_updates.copy()
    if include_skipped:
        all_updates.extend(skipped_updates)

    # Group by category
    categories: dict[str, list[dict[str, Any]]] = {
        "core": [],
        "os": [],
        "supervisor": [],
        "addons": [],
        "hacs": [],
        "devices": [],
        "other": [],
    }

    for update in all_updates:
        category = update.get("category", "other")
        if category in categories:
            categories[category].append(update)
        else:
            categories["other"].append(update)

    categories = {k: v for k, v in categories.items() if v}

    result: dict[str, Any] = {
        "success": True,
        "updates_available": len(available_updates),
        "skipped_count": len(skipped_updates),
        "updates": all_updates,
        "categories": categories,
        "include_skipped": include_skipped,
    }

    return result


def update_details(
    entity_state: dict[str, Any], *, include_attributes: bool = False
) -> dict[str, Any]:
    entity_id = entity_state["entity_id"]
    attributes = entity_state.get("attributes", {})
    latest_version = attributes.get("latest_version", "unknown")
    state = entity_state.get("state", "")
    result: dict[str, Any] = {
        "success": True,
        "entity_id": entity_id,
        "title": attributes.get("title", entity_id),
        "state": state,
        "update_available": state == "on",
        "installed_version": attributes.get("installed_version"),
        "latest_version": latest_version,
        "release_summary": attributes.get("release_summary"),
        "release_url": attributes.get("release_url"),
        "can_install": not attributes.get("in_progress", False),
        "in_progress": attributes.get("in_progress", False),
        "skipped_version": attributes.get("skipped_version"),
        "auto_update": attributes.get("auto_update", False),
        "category": _categorize_update(entity_id, attributes),
    }

    if include_attributes:
        result["attributes"] = attributes
    return result


def update_batch_result(
    action: str, requested: int, results: list[dict[str, Any]], succeeded: int
) -> dict[str, Any]:
    failed = requested - succeeded
    response: dict[str, Any] = {
        "success": failed == 0,
        "action": action,
        "requested": requested,
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }
    if not requested:
        response["note"] = "No matching pending updates to act on."
    elif action == "install" and succeeded:
        response["note"] = (
            "Confirm per entity with ha_manage_updates(action='get') that "
            "installed_version reached latest_version."
        )
    return response
