"""Qualification cannot silently install substituted bytes or select legacy."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from qualification_artifacts import (
    stage_candidate_component,
    verify_candidate_artifacts,
)
from src.haos_candidate import _configure_entry


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir()
    wheel = assets / "ha_mcp-9.0.0.dev1-py3-none-any.whl"
    wheel.write_bytes(b"candidate wheel bytes")
    dependency = f"ha-mcp @ https://github.com/example/fork/{wheel.name}"
    with zipfile.ZipFile(assets / "ha_mcp_tools.zip", "w") as archive:
        archive.writestr(
            "manifest.json", json.dumps({"requirements": [dependency, "mcp>=1.24.0"]})
        )
    proof = {
        "revision": "candidate-revision",
        "library_version": "9.0.0.dev1",
        "dependency": dependency,
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in assets.iterdir()
        },
    }
    (assets / "native-build.json").write_text(json.dumps(proof))
    return assets


def test_rejects_replaced_wheel_before_staging(candidate: Path, tmp_path: Path) -> None:
    next(candidate.glob("*.whl")).write_bytes(b"different code")
    with pytest.raises(ValueError, match="SHA-256"):
        stage_candidate_component(
            candidate, tmp_path / "component", tmp_path / "config"
        )
    assert not (tmp_path / "component").exists()


def test_rejects_assets_from_another_checkout(candidate: Path) -> None:
    with pytest.raises(ValueError, match="another source revision"):
        verify_candidate_artifacts(candidate, expected_revision="other-revision")


def test_core_installs_paired_local_bytes_with_original_requirements(
    candidate: Path, tmp_path: Path
) -> None:
    component = tmp_path / "component"
    wheel = stage_candidate_component(candidate, component, tmp_path / "config")
    requirements = json.loads((component / "manifest.json").read_text())["requirements"]
    assert "mcp>=1.24.0" in requirements
    assert (
        f"#sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()}" in requirements[0]
    )
    assert requirements[0].startswith("ha-mcp @ file:///config/")
    assert wheel.read_bytes() == next(candidate.glob("*.whl")).read_bytes()


def test_native_staging_cannot_retain_legacy_credentials(tmp_path: Path) -> None:
    entry = {
        "entry_id": "e2e_test_ha_mcp_server_entry",
        "data": {"admin_token": "legacy-token"},
        "options": {"pip_spec": "old", "server_port": 9584, "bind_host": "127.0.0.1"},
    }
    document = {"data": {"entries": [entry]}}
    auth = {
        "data": {
            "users": [
                {
                    "id": "owner",
                    "is_owner": True,
                    "is_active": True,
                    "system_generated": False,
                }
            ]
        }
    }
    _configure_entry(document, auth, "native", tmp_path / "wheel.whl", "unused")
    assert entry["data"] == {"native_owner_user_id": "owner"}
    assert entry["options"] == {"runtime_mode": "native"}


def test_native_staging_rejects_system_generated_owner(tmp_path: Path) -> None:
    document = {
        "data": {"entries": [{"entry_id": "e2e_test_ha_mcp_server_entry", "data": {}}]}
    }
    auth = {
        "data": {
            "users": [
                {
                    "id": "system",
                    "is_owner": True,
                    "is_active": True,
                    "system_generated": True,
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="active human owner"):
        _configure_entry(document, auth, "native", tmp_path / "wheel.whl", "unused")
