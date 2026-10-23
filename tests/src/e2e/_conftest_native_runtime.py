"""Explicit native staging and runtime proof for the embedded E2E lane."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
    return observed


__all__ = ["native_runtime_selected"]
