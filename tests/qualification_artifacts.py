"""Paired, immutable candidate artifacts for disposable Core qualification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any


def native_runtime_selected() -> bool:
    """Require an explicit runtime selection and reject misspelled lanes."""
    value = os.environ.get("E2E_EMBEDDED_RUNTIME", "legacy")
    if value not in {"legacy", "native"}:
        raise ValueError(f"Unknown embedded runtime: {value!r}")
    return value == "native"


def build_candidate_artifacts(repository: Path, output: Path) -> Path:
    """Build the native release profile from this checkout's exact commit."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    # Unique per revision, without creating a tag or publishing any asset.
    version = f"9.0.0.dev{int(revision[:12], 16)}"
    subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/native_release.py"),
            "--version",
            version,
            "--tag",
            f"qualification-{revision}",
            "--github",
            "julienld/ha-mcp-native",
            "--revision",
            revision,
            "--output",
            str(output),
        ],
        cwd=repository,
        check=True,
        timeout=600,
    )
    verify_candidate_artifacts(output, expected_revision=revision)
    return output


def verify_candidate_artifacts(
    assets: Path, *, expected_revision: str | None = None
) -> dict[str, Any]:
    """Reject missing, substituted or mismatched qualification assets."""
    proof = json.loads((assets / "native-build.json").read_text(encoding="utf-8"))
    if expected_revision is not None and proof["revision"] != expected_revision:
        raise ValueError("Qualification assets belong to another source revision")
    wheel = f"ha_mcp-{proof['library_version']}-py3-none-any.whl"
    for name in (wheel, "ha_mcp_tools.zip"):
        path = assets / name
        if (
            path.name != name
            or hashlib.sha256(path.read_bytes()).hexdigest() != proof["files"][name]
        ):
            raise ValueError(
                f"Qualification artifact failed SHA-256 verification: {name}"
            )
    return proof


def stage_candidate_component(
    assets: Path,
    component: Path,
    wheel_directory: Path,
    *,
    guest_wheel_directory: str = "/config",
) -> Path:
    """Stage paired code and a local wheel requirement for Core's installer.

    Only the disposable copy's URL changes. Requirements, wheel bytes and the
    native file index remain those produced by the release builder. Core still
    installs each requirement through its own constrained installer.
    """
    proof = verify_candidate_artifacts(assets)
    wheel = assets / f"ha_mcp-{proof['library_version']}-py3-none-any.whl"
    component.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(assets / "ha_mcp_tools.zip") as archive:
        for member in archive.infolist():
            target = (component / member.filename).resolve()
            if not target.is_relative_to(component.resolve()):
                raise ValueError("Qualification archive contains an unsafe path")
        archive.extractall(component)
    manifest_path = component / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requirement = proof["dependency"]
    if manifest["requirements"].count(requirement) != 1:
        raise ValueError("Integration and library are not a paired candidate")
    local_requirement = (
        f"ha-mcp @ file://{guest_wheel_directory.rstrip('/')}/{wheel.name}"
        f"#sha256={proof['files'][wheel.name]}"
    )
    manifest["requirements"] = [
        local_requirement if value == requirement else value
        for value in manifest["requirements"]
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    wheel_directory.mkdir(parents=True, exist_ok=True)
    staged_wheel = wheel_directory / wheel.name
    shutil.copyfile(wheel, staged_wheel)
    shutil.copyfile(assets / "native-build.json", wheel_directory / "native-build.json")
    return staged_wheel
