"""Verify installed wheel bytes against the component's independent build index.

Called only in an executor. It neither imports the installed library nor trusts
its RECORD, and performs no installation, cache writes, or network access.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from importlib.metadata import Distribution
from pathlib import Path, PurePosixPath
from typing import Any

MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_FILES = 10_000
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
INDEX_PATH = Path(__file__).with_name("native-wheel.json")
_INSTALLER_FILES = {
    "RECORD",
    "INSTALLER",
    "direct_url.json",
    "REQUESTED",
    "uv_cache.json",
    "uv_build.json",
}


def loaded_origins_match(distribution: Distribution) -> bool:
    """Reject shadow or mixed installed roots without importing any HA-MCP module."""
    root = Path(str(distribution.locate_file("ha_mcp"))).resolve(strict=True)
    loaded = {
        name: module
        for name, module in sys.modules.copy().items()
        if name == "ha_mcp" or name.startswith("ha_mcp.")
    }
    if loaded and "ha_mcp" not in loaded:
        return False
    for name, module in loaded.items():
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            return False
        origin = Path(filename).resolve(strict=True)
        if not origin.is_relative_to(root) or (
            name == "ha_mcp" and origin != root / "__init__.py"
        ):
            return False
        spec_origin = getattr(getattr(module, "__spec__", None), "origin", None)
        if spec_origin is not None and (
            not isinstance(spec_origin, str)
            or Path(spec_origin).resolve(strict=True) != origin
        ):
            return False
        paths = getattr(module, "__path__", None)
        if paths is not None and any(
            Path(path).resolve(strict=True) != origin.parent for path in paths
        ):
            return False
    return True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate payload-index key")
        result[key] = value
    return result


def _read_index(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Payload index must be a regular component file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_INDEX_BYTES + 1)
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError("Payload index exceeds its size bound")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("Expected a payload-index object")
    return value


def _members(index: dict[str, Any], distribution: str, version: str) -> dict[str, Any]:
    info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    files = index.get("files")
    if not isinstance(files, dict) or not 3 <= len(files) <= MAX_FILES:
        raise ValueError("Invalid payload-index file count")
    if not {"ha_mcp/__init__.py", f"{info}/METADATA", f"{info}/WHEEL"} <= files.keys():
        raise ValueError("Payload index omits package identity files")
    total = 0
    for name, entry in files.items():
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != name
            or len(path.parts) < 2
            or path.parts[0] not in {"ha_mcp", info}
            or (path.parts[0] == info and path.name in _INSTALLER_FILES)
            or path.suffix == ".pyc"
            or "__pycache__" in path.parts
        ):
            raise ValueError("Unsafe or installer-generated payload-index member")
        if (
            not isinstance(entry, dict)
            or set(entry) != {"size", "sha256"}
            or type(entry["size"]) is not int
            or entry["size"] < 0
            or not isinstance(entry["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]) is None
        ):
            raise ValueError("Invalid payload-index file description")
        total += entry["size"]
        if total > MAX_PAYLOAD_BYTES:
            raise ValueError("Payload index exceeds its total-byte bound")
    return files


def _file_matches(path: Path, size: int, digest: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != size:
            return False
        hashed = hashlib.sha256()
        remaining = size
        while remaining:
            block = stream.read(min(remaining, 128 * 1024))
            if not block:
                return False
            hashed.update(block)
            remaining -= len(block)
        after = os.fstat(stream.fileno())

    def identity(item: os.stat_result) -> tuple[int, int, int, int]:
        return (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
        )

    return (
        identity(before) == identity(after) == identity(path.stat())
        # Windows fstat/stat can report different creation-time precision. ctime
        # remains useful between reads of the same handle, not across those APIs.
        and before.st_ctime_ns == after.st_ctime_ns
        and hashed.hexdigest() == digest
    )


def _package_inventory(root: Path) -> set[str]:
    members: set[str] = set()
    visited = 0
    for directory, dirs, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        if any((current / name).is_symlink() for name in dirs):
            raise ValueError("Installed package contains a symlink directory")
        visited += len(dirs) + len(filenames)
        if visited > MAX_FILES * 3:
            raise ValueError("Installed package inventory exceeds its bound")
        for name in filenames:
            path = current / name
            if path.is_symlink():
                raise ValueError("Installed package contains a symlink file")
            relative = path.relative_to(root.parent).as_posix()
            if current.name == "__pycache__" and path.suffix == ".pyc":
                continue
            members.add(relative)
    return members


def verify_wheel_payload(
    distribution: Distribution,
    name: str,
    version: str,
    wheel_sha256: str,
    *,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Prove installed payload equality; this does not fabricate an archive receipt."""
    index = _read_index(index_path if index_path is not None else INDEX_PATH)
    if (
        type(index.get("format_version")) is not int
        or index["format_version"] != 1
        or index.get("distribution") != name
        or index.get("version") != version
        or index.get("wheel_sha256") != wheel_sha256
    ):
        raise ValueError("Payload index does not identify this exact paired wheel")
    files = _members(index, name, version)
    site = Path(str(distribution.locate_file(""))).resolve(strict=True)
    root = Path(str(distribution.locate_file("ha_mcp")))
    if root.is_symlink() or root.resolve(strict=True) != site / "ha_mcp":
        raise ValueError("Installed package root escapes its distribution")
    if not loaded_origins_match(distribution):
        raise ValueError("Loaded HA-MCP modules have a different origin")
    for member, expected in files.items():
        path = Path(str(distribution.locate_file(member)))
        resolved = path.resolve(strict=True)
        if resolved != site / member or not _file_matches(
            path, expected["size"], expected["sha256"]
        ):
            raise ValueError("Installed wheel payload differs from the component index")
    expected_package = {member for member in files if member.startswith("ha_mcp/")}
    if _package_inventory(root) != expected_package:
        raise ValueError("Installed package has extra or missing payload files")
    if not loaded_origins_match(distribution):
        raise ValueError("Loaded HA-MCP origin changed during payload verification")
    return {
        "verified": True,
        "proof": "component_payload_index",
        "files_checked": len(files),
        "bytes_checked": sum(entry["size"] for entry in files.values()),
    }
