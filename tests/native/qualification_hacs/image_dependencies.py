"""Build-time requirements for an image which cannot install at runtime."""

from __future__ import annotations

import json
from pathlib import Path


def install_manifest_requirements(components: Path, constraints: Path) -> None:
    from homeassistant.util import package

    requirements = [
        requirement
        for domain in ("hacs", "ha_mcp_tools")
        for requirement in json.loads(
            (components / domain / "manifest.json").read_text(encoding="utf-8")
        )["requirements"]
    ]
    for requirement in requirements:
        if not package.is_installed(requirement) and not package.install_package(
            requirement, constraints=str(constraints)
        ):
            raise RuntimeError(f"Build could not install {requirement}")
    # Check together: installing a later requirement must not invalidate one
    # already processed. Core's runtime installer must have no work left.
    missing = [req for req in requirements if not package.is_installed(req)]
    if missing:
        raise RuntimeError(f"Unsatisfied runtime requirements: {missing}")
