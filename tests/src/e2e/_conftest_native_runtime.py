"""Explicit native staging and runtime proof for the embedded E2E lane."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests

# pytest's console entry point can load this before tests/ is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qualification_artifacts import (
    build_candidate_artifacts,
    native_runtime_selected,
    stage_candidate_component,
    verify_candidate_artifacts,
)

NATIVE_DEPENDENCY_PROOF = "native-dependencies.json"
_DEPENDENCY_HARNESS = "native-dependency-harness"


def stage_native_dependency_capture(repository: Path, config: Path) -> None:
    """Mount checkout test code without changing the attested component/wheel."""
    destination = config / _DEPENDENCY_HARNESS
    destination.mkdir()
    shutil.copy2(
        repository / "tests/fixtures/native_qualification/dependency_capture.py",
        destination,
    )
    for name in ("dependency_receipt.py", "dependency_policy.py"):
        shutil.copy2(
            repository / "tests/native/fixtures/native_fixture" / name, destination
        )


def native_dependency_command(phase: str) -> list[str]:
    return [
        "python3",
        f"/config/{_DEPENDENCY_HARNESS}/dependency_capture.py",
        phase,
        "/config",
    ]


def capture_native_dependencies(container: Any, phase: str) -> dict[str, Any]:
    result = container.get_wrapped_container().exec_run(
        native_dependency_command(phase)
    )
    output = (result.output or b"").decode("utf-8", "replace")
    # A refused observation is still evidence and belongs in the CI artifact.
    try:
        proof = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise AssertionError(
            f"Native dependency capture failed: {output[-2000:]}"
        ) from error
    _save_native_evidence(proof, "native-dependencies")
    assert result.exit_code == 0, f"Native dependency policy refused {phase}: {output}"
    return proof


def assert_native_dependency_policy(config: Path, *, runtime: bool = False) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "fixtures/native_qualification/dependency_capture.py"
    )
    proof = json.loads((config / NATIVE_DEPENDENCY_PROOF).read_text(encoding="utf-8"))
    _save_native_evidence(proof, "native-dependencies")
    runpy.run_path(str(source))["assert_qualified"](proof, runtime=runtime)


def stage_native_runtime(repository: Path, config: Path) -> Path:
    """Use the exact committed candidate and let Core install its requirements."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    selected = os.environ.get("E2E_NATIVE_ARTIFACTS")
    assets = (
        Path(selected).resolve()
        if selected
        else build_candidate_artifacts(repository, config / "qualification-assets")
    )
    verify_candidate_artifacts(assets, expected_revision=revision)
    wheel = stage_candidate_component(
        assets, config / "custom_components/ha_mcp_tools", config
    )
    shutil.copytree(
        repository / "tests/fixtures/native_qualification",
        config / "custom_components/native_qualification",
        dirs_exist_ok=True,
    )
    stage_native_dependency_capture(repository, config)
    with (config / "configuration.yaml").open("a", encoding="utf-8") as stream:
        stream.write("\nnative_qualification:\n")
    return wheel


def configure_native_entry(config: Path, entry_id: str) -> None:
    """Attribute native calls to the fixture's actual active human owner."""
    auth = json.loads((config / ".storage/auth").read_text(encoding="utf-8"))
    owners = [
        user
        for user in auth["data"]["users"]
        if user.get("is_owner") is True
        and user.get("is_active") is True
        and not user.get("system_generated")
        and "system-admin" in user.get("group_ids", [])
    ]
    if len(owners) != 1:
        raise ValueError("Native qualification requires one active human fixture owner")
    path = config / ".storage/core.config_entries"
    storage = json.loads(path.read_text(encoding="utf-8"))
    entry = next(
        row for row in storage["data"]["entries"] if row["entry_id"] == entry_id
    )
    entry["data"].pop("admin_token", None)
    entry["data"]["native_owner_user_id"] = owners[0]["id"]
    entry["options"].update(
        runtime_mode="native",
        enable_llm_api=False,
        enable_native_llm_import=False,
    )
    path.write_text(json.dumps(storage, indent=2) + "\n", encoding="utf-8")


def _save_native_attestation(observed: dict[str, Any]) -> None:
    selected = os.environ.get("E2E_ATTESTATION_DIRECTORY")
    if not selected:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    if re.fullmatch(r"gw[0-9]+|master", worker) is None:
        raise ValueError("Native attestation worker identity is invalid")
    fields = (
        "core_version",
        "python_version",
        "architecture",
        "runtime",
        "revision",
        "library_version",
    )
    for key in fields:
        if not isinstance(observed.get(key), str) or not observed[key]:
            raise AssertionError(f"Native attestation has no actual {key}")
    receipt = {key: observed[key] for key in fields}
    receipt.update(worker=worker, on_core_loop=observed["on_core_loop"])
    github = {
        key: os.environ[name]
        for key, name in (
            ("sha", "GITHUB_SHA"),
            ("run_id", "GITHUB_RUN_ID"),
            ("run_attempt", "GITHUB_RUN_ATTEMPT"),
            ("job", "GITHUB_JOB"),
        )
        if os.environ.get(name)
    }
    if github:
        receipt["github"] = github
    _save_native_evidence(receipt, "native-runtime")


def _save_native_evidence(receipt: dict[str, Any], prefix: str) -> None:
    selected = os.environ.get("E2E_ATTESTATION_DIRECTORY")
    if not selected:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    if re.fullmatch(r"gw[0-9]+|master", worker) is None:
        raise ValueError("Native attestation worker identity is invalid")
    directory = Path(selected).expanduser().resolve()
    destination = directory / f"{prefix}-{worker}.json"
    if destination.resolve().parent != directory:
        raise ValueError("Native attestation path escaped its output directory")
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(receipt, indent=2) + "\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def attest_native_runtime(base_url: str, headers: dict[str, str]) -> dict[str, Any]:
    """A responsive webhook alone must never qualify the wrong runtime."""
    response = requests.get(
        base_url + "/api/native_qualification", headers=headers, timeout=30
    )
    response.raise_for_status()
    observed: dict[str, Any] = response.json()
    expected = {
        "manager": "NativeServerManager",
        "runtime": "native",
        "running": True,
        "on_core_loop": True,
        "legacy_listener": False,
        "legacy_thread": False,
        "admin_token_present": False,
    }
    if any(observed.get(key) != value for key, value in expected.items()):
        raise AssertionError(
            f"Embedded lane did not attest a native runtime: {observed}"
        )
    if not observed.get("revision") or not observed.get("library_version"):
        raise AssertionError(
            "Native runtime did not provide its paired artifact identity"
        )
    expected_core = os.environ.get("E2E_EXPECTED_CORE_VERSION")
    if expected_core and observed.get("core_version") != expected_core:
        raise AssertionError(
            f"Core version mismatch: expected {expected_core}, "
            f"observed {observed.get('core_version')}"
        )
    expected_arch = os.environ.get("E2E_EXPECTED_ARCH", "").lower()
    architecture = str(observed.get("architecture", "")).lower()
    aliases = {"x64": "x86_64", "amd64": "x86_64", "arm64": "aarch64"}
    if expected_arch and aliases.get(expected_arch, expected_arch) != aliases.get(
        architecture, architecture
    ):
        raise AssertionError(
            f"Core architecture mismatch: expected {expected_arch}, observed {architecture}"
        )
    _save_native_attestation(observed)
    return observed


__all__ = ["native_runtime_selected"]
