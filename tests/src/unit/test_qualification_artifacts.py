"""Qualification cannot silently install substituted bytes or select legacy."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from tests.qualification_artifacts import (
    stage_candidate_component,
    verify_candidate_artifacts,
)
from tests.src.haos_candidate import _configure_entry

from .test_native_release import release_builder as release_builder
from .test_native_release import source as source


def test_qualification_build_survives_a_new_core_floor_without_becoming_publishable(
    source, release_builder, monkeypatch, tmp_path
):
    from tests import qualification_artifacts

    monkeypatch.setenv("GITHUB_REPOSITORY", "example/qualification-fork")
    monkeypatch.setattr(
        qualification_artifacts.subprocess,
        "check_output",
        lambda *args, **kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        release_builder.core_window,
        "resolve_window",
        lambda: release_builder.core_window.make_window(
            "2026.12.1", {"2026.11.0": "3.15.1", "2026.12.1": "3.15.2"}
        ),
    )
    wheel_build = qualification_artifacts.subprocess.run

    def run(args, **kwargs):
        if len(args) > 1 and str(args[1]).endswith("native_release.py"):

            def value(flag):
                return args[args.index(flag) + 1]

            return release_builder.build(
                source,
                Path(value("--output")),
                value("--version"),
                value("--tag"),
                value("--github"),
                qualification_only="--qualification-only" in args,
            )
        return wheel_build(args, **kwargs)

    monkeypatch.setattr(qualification_artifacts.subprocess, "run", run)
    output = qualification_artifacts.build_candidate_artifacts(
        source, tmp_path / "build"
    )
    proof = qualification_artifacts.verify_candidate_artifacts(output)
    assert proof["qualification_only"] is True
    assert proof["repository"] == "example/qualification-fork"


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


def test_rejects_non_object_provenance_before_staging(
    candidate: Path, tmp_path: Path
) -> None:
    (candidate / "native-build.json").write_text("[]")
    component = tmp_path / "component"
    with pytest.raises(ValueError, match="JSON object"):
        stage_candidate_component(candidate, component, tmp_path / "config")
    assert not component.exists()


def test_staging_contains_only_the_candidate_archive_not_checkout_leftovers(
    candidate: Path, tmp_path: Path
) -> None:
    component = tmp_path / "component"
    (component / "old_package/__pycache__").mkdir(parents=True)
    (component / "obsolete.py").write_text("old implementation")
    (component / "old_package/__pycache__/old.pyc").write_bytes(b"old cache")
    sibling = tmp_path / "unrelated.txt"
    sibling.write_text("keep")
    stage_candidate_component(candidate, component, tmp_path / "config")
    with zipfile.ZipFile(candidate / "ha_mcp_tools.zip") as archive:
        expected = {name for name in archive.namelist() if not name.endswith("/")}
    assert {
        p.relative_to(component).as_posix() for p in component.rglob("*") if p.is_file()
    } == expected
    assert sibling.read_text() == "keep"


def test_unsafe_archive_is_rejected_before_existing_component_is_removed(
    candidate: Path, tmp_path: Path
) -> None:
    component = tmp_path / "component"
    component.mkdir()
    existing = component / "keep.py"
    existing.write_text("existing")
    with zipfile.ZipFile(candidate / "ha_mcp_tools.zip", "a") as archive:
        archive.writestr("../escape.txt", "unsafe")
    proof_path = candidate / "native-build.json"
    proof = json.loads(proof_path.read_text())
    proof["files"]["ha_mcp_tools.zip"] = hashlib.sha256(
        (candidate / "ha_mcp_tools.zip").read_bytes()
    ).hexdigest()
    proof_path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match="unsafe path"):
        stage_candidate_component(candidate, component, tmp_path / "config")
    assert existing.read_text() == "existing"
    assert not (tmp_path / "escape.txt").exists()


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
