"""Recognize a paired fork build without accepting a caller-selected origin."""

import importlib.metadata
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.version import Version

from ha_mcp._version import _owns_ha_mcp

from .paired_wheel import paired_wheel_matches


def valid_repository(name: str) -> bool:
    return (
        isinstance(name, str)
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name
        )
        is not None
    )


def paired_fork_origin(repository: str, installed: str) -> bool:
    """Borrow the startup wheel proof: exact URL/version/hash or indexed bytes."""
    if not valid_repository(repository):
        return False
    try:
        manifest = json.loads(
            Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
        )
        if manifest.get("domain") != "ha_mcp_tools" or Version(
            manifest["version"]
        ) != Version(installed):
            return False
        if (
            manifest.get("documentation", "").rstrip("/").casefold()
            != f"https://github.com/{repository}".casefold()
        ):
            return False
        owners = [name for name in ("ha-mcp", "ha-mcp-dev") if _owns_ha_mcp(name)]
        if len(owners) != 1:
            return False
        specs = [
            spec
            for spec in manifest["requirements"]
            if Requirement(spec).name == owners[0]
        ]
        if len(specs) != 1:
            return False
        url = urlsplit(Requirement(specs[0]).url or "")
        if url.hostname != "github.com" or not url.path.casefold().startswith(
            f"/{repository}/releases/download/".casefold()
        ):
            return False
        distribution = importlib.metadata.distribution(owners[0])
        return paired_wheel_matches(specs[0], distribution.version, manifest["version"])
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        importlib.metadata.PackageNotFoundError,
    ):
        return False
