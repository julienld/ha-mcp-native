"""Bounded skill-file resolution; the entry supplies its feature flag."""

from typing import Any

from ha_mcp.utils.skill_loader import (
    BEST_PRACTICES_SKILL_NAME,
    get_skills_dir,
    resolve_skill_files,
)


def read_skill_files(references: list[str], *, enabled: bool = True) -> dict[str, Any]:
    if not enabled:
        return {"content": {}, "enabled": False, "vendor_missing": False}
    bare_paths = {reference for reference in references if "#" not in reference}
    wanted = sorted(
        {
            reference
            for reference in references
            if "#" not in reference or reference.split("#", 1)[0] not in bare_paths
        }
    )
    directory = get_skills_dir()
    return {
        "content": resolve_skill_files(directory, BEST_PRACTICES_SKILL_NAME, wanted),
        "enabled": True,
        "vendor_missing": directory is None,
    }
