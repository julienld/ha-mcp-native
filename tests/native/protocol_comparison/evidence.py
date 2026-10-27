"""Source/build receipts and bounded, allowlisted failure evidence."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .protocol_fixture.provenance import file_digest, tree_digest, wheel_package_digest

PROTECTED = ("src", "custom_components", "pyproject.toml", "uv.lock")


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=source, text=True).strip()


def build_receipt(
    source: Path,
    revision: str,
    component: Path,
    wheel: Path,
    core_image_id: str,
    run_nonce: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("full_committed_revision_required")
    if git(source, "rev-parse", "HEAD") != revision:
        raise ValueError("source_revision_mismatch")
    if git(source, "status", "--porcelain", "--untracked-files=all", "--", *PROTECTED):
        raise ValueError("dirty_application_source")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", core_image_id):
        raise ValueError("immutable_core_image_id_required")
    if not re.fullmatch(r"[a-f0-9]{32}", run_nonce):
        raise ValueError("isolated_run_nonce_required")
    return {
        "revision": revision,
        "source_clean_at_receipt": True,
        "source_trees": {p: git(source, "rev-parse", f"HEAD:{p}") for p in PROTECTED},
        "core_image_id": core_image_id,
        "run_nonce": run_nonce,
        "component_sha256": tree_digest(component),
        "wheel_sha256": file_digest(wheel),
        "package_sha256": wheel_package_digest(wheel),
        "wheel_name": wheel.name,
        "provenance_limit": "Builder asserts artifact origin; hashes prove installed byte identity.",
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def validate_runtime(
    observed: dict[str, Any], expected: dict[str, Any]
) -> dict[str, Any]:
    equal = (
        "run_nonce",
        "revision",
        "core_image_id",
        "component_sha256",
        "wheel_sha256",
        "package_sha256",
    )
    if any(observed.get(key) != expected[key] for key in equal):
        raise ValueError("runtime_build_receipt_mismatch")
    for key in (
        "running",
        "source_clean_at_receipt",
        "component_matches_build",
        "package_matches_build",
        "private_sdk_loaded",
    ):
        if observed.get(key) is not True:
            raise ValueError("runtime_attestation_failed")
    if observed["runtime"] != expected["runtime"]:
        raise ValueError("unexpected_runtime")
    if observed["entry_runtime"] != expected["runtime"]:
        raise ValueError("entry_runtime_mismatch")
    if observed["private_versions"] != {
        "fastmcp": "4.1.2",
        "mcp": "2.3.0",
        "mcp_types": "2.3.0",
    }:
        raise ValueError("comparison_sdk_versions_not_aligned")
    fields = (
        "revision",
        "core_image_id",
        "component_sha256",
        "wheel_sha256",
        "package_sha256",
        "core_version",
        "python_version",
        "architecture",
        "runtime",
        "manager_type",
        "ha_mcp_version",
        "private_versions",
        "component_matches_build",
        "package_matches_build",
        "private_sdk_loaded",
    )
    return {field: observed[field] for field in fields}


def scan_log(path: Path, offset: int) -> dict[str, Any]:
    """Read only bytes appended during the measured window; never export log text."""
    size = path.stat().st_size
    if size < offset:
        raise ValueError("server_log_rotated")
    if size - offset > 16 * 1024 * 1024:
        raise ValueError("server_log_window_limit")
    with path.open("rb") as stream:
        stream.seek(offset)
        text = stream.read().decode("utf-8", errors="replace")
    patterns = {
        "asgi_failure": r"ASGI.*(?:error|exception)|(?:error|exception).*ASGI",
        "sse_failure": r"(?:SSE|event.stream).*(?:error|exception)|(?:error|exception).*(?:SSE|event.stream)",
        "empty_body_send": r"Cannot call write\(\) after write_eof|Unexpected ASGI message.*http.response.body",
        "connection_failure": r"ClientConnectionResetError|RemoteProtocolError|BrokenResourceError|ClosedResourceError",
        "traceback": r"Traceback \(most recent call last\)",
    }
    return {
        "bytes_scanned": size - offset,
        "capture_markers": text.count("PROTOCOL_COMPARISON_CAPTURE"),
        "counts": {
            name: len(re.findall(pattern, text, flags=re.IGNORECASE))
            for name, pattern in patterns.items()
        },
        "raw_text_exported": False,
    }
