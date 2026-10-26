"""Attest the running image, not the Python patch used by Core's source build."""

import importlib.metadata
import json
import platform
import re
import sys
import sysconfig
from pathlib import Path
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ARCHITECTURES = {"x86_64": "amd64", "arm64": "aarch64", "aarch64": "aarch64"}


def inspect_runtime() -> dict[str, Any]:
    core = importlib.metadata.distribution("homeassistant")
    return {
        "core": core.version,
        "core_requires_python": core.metadata.get("Requires-Python"),
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "cache_tag": sys.implementation.cache_tag,
        "soabi": sysconfig.get_config_var("SOABI"),
        "arch": ARCHITECTURES.get(platform.machine(), platform.machine()),
    }


def attest_runtime(
    receipt: Path,
    *,
    expected_core: str,
    expected_arch: str,
    python_source: str,
    requires_python: str,
) -> dict[str, Any]:
    """The caller must run the inspected immutable image ID, never re-resolve a tag."""
    images = json.loads(receipt.read_text(encoding="utf-8"))
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("Expected one Docker image receipt")
    image = images[0]
    image_id = image.get("Id", "")
    digests = image.get("RepoDigests", [])
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or not digests:
        raise ValueError("Missing immutable image identity/digest")
    if not all(re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", d) for d in digests):
        raise ValueError("Invalid image repository digest")
    image_arch = ARCHITECTURES.get(image.get("Architecture"), image.get("Architecture"))
    actual = inspect_runtime()
    if image.get("Os") != "linux" or image_arch != expected_arch:
        raise ValueError("Docker image platform does not match the matrix")
    if actual["core"] != expected_core or actual["arch"] != expected_arch:
        raise ValueError(f"Core/architecture mismatch: {actual}")
    python = Version(actual["python"])
    for owner, constraint in (
        ("Core", actual["core_requires_python"]),
        ("ha-mcp", requires_python),
    ):
        if not constraint or python not in SpecifierSet(constraint):
            raise ValueError(
                f"Image Python {python} violates {owner} Requires-Python {constraint!r}"
            )
    abi = f"cpython-{python.major}{python.minor}"
    if (
        actual["implementation"] != "cpython"
        or actual["cache_tag"] != abi
        or not str(actual["soabi"]).startswith(abi + "-")
    ):
        raise ValueError(f"Incompatible image Python ABI: {actual}")
    return {
        "verified": True,
        "image_id": image_id,
        "repo_digests": digests,
        "runtime": actual,
        "ha_mcp_requires_python": requires_python,
        "python_source_hint": python_source,
    }
