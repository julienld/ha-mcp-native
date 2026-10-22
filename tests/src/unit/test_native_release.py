"""Candidate archives must select and identify the exact paired distribution."""

import importlib.util
import json
import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from packaging.version import Version


@pytest.fixture
def release_builder():
    path = Path(__file__).resolve().parents[3] / "scripts/native_release.py"
    spec = importlib.util.spec_from_file_location("native_release_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path, release_builder, monkeypatch):
    root = tmp_path / "source"
    component = root / "custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    (root / "pyproject.toml").write_text("""[project]
name = "ha-mcp"
version = "8.7.0"
dependencies = [
    "httpx>=0.27",
    "rich>=15",
]
[project.scripts]
ha-mcp = "ha_mcp:main"
""")
    (root / "hacs.json").write_text(
        json.dumps(
            {"name": "HA-MCP", "homeassistant": "2026.8.0", "render_readme": True}
        )
    )
    (root / "LICENSE").write_text("fixture license\n")
    (root / "README.md").write_text("upstream readme\n")
    (component / "manifest.json").write_text(
        json.dumps(
            {
                "domain": "ha_mcp_tools",
                "name": "HA-MCP",
                "version": "8.7.0",
                "requirements": ["ha-mcp==8.7.0", "ruamel.yaml>=0.18"],
            }
        )
    )
    (component / "const.py").write_text(
        'COMPONENT_VERSION = "8.7.0"\nMIN_NATIVE_HOME_ASSISTANT_VERSION = "2026.10.2"\n'
    )
    (component / "__init__.py").write_text("# fixture component\n")
    monkeypatch.setattr(release_builder, "run", lambda *args, **kwargs: "a" * 40)

    def stage(repository, staging, revision):
        shutil.copytree(repository, staging, dirs_exist_ok=True)
        return {"src/ha_mcp/skills/vendor": "b" * 40}

    def wheel_build(args, *, cwd, **kwargs):
        output = Path(args[args.index("--out-dir") + 1])
        output.mkdir(parents=True, exist_ok=True)
        version = tomllib.loads((cwd / "pyproject.toml").read_text())["project"][
            "version"
        ]
        with zipfile.ZipFile(
            output / f"ha_mcp-{version}-py3-none-any.whl", "w"
        ) as wheel:
            wheel.writestr(
                f"ha_mcp-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.3\nName: ha-mcp\nVersion: {version}\n",
            )
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(release_builder, "stage_revision", stage)
    monkeypatch.setattr(release_builder.subprocess, "run", wheel_build)
    return root


def test_each_candidate_has_a_unique_manifest_version_and_numeric_legacy_protocol(
    source, tmp_path, release_builder
):
    versions = []
    for version in ("9.0.0.dev9", "9.0.0.dev10"):
        output = tmp_path / version
        release_builder.build(
            source, output, version, version, "julienld/ha-mcp-native"
        )
        with zipfile.ZipFile(output / "ha_mcp_tools.zip") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            versions.append(manifest["version"])
            assert 'COMPONENT_VERSION = "9.0.0"' in archive.read("const.py").decode()
    assert Version(versions[0]) < Version(versions[1]) < Version("9.0.0")


def test_component_dependency_hash_binds_the_exact_wheel(
    source, tmp_path, release_builder
):
    output = tmp_path / "candidate"
    proof = release_builder.build(
        source, output, "9.0.0.dev1", "9.0.0.dev1", "julienld/ha-mcp-native"
    )
    filename = "ha_mcp-9.0.0.dev1-py3-none-any.whl"
    with zipfile.ZipFile(output / "ha_mcp_tools.zip") as archive:
        requirement = json.loads(archive.read("manifest.json"))["requirements"][0]
    assert requirement.endswith("#sha256=" + proof["files"][filename])


def test_existing_candidate_is_never_overwritten(source, tmp_path, release_builder):
    output = tmp_path / "candidate"
    output.mkdir()
    marker = output / "ha_mcp_tools.zip"
    marker.write_bytes(b"previous-reviewed-candidate")
    with pytest.raises(FileExistsError):
        release_builder.build(
            source, output, "9.0.0.dev1", "9.0.0.dev1", "julienld/ha-mcp-native"
        )
    assert marker.read_bytes() == b"previous-reviewed-candidate"


def test_stamping_requires_exactly_one_legacy_protocol_constant(
    source, release_builder
):
    component = source / "custom_components/ha_mcp_tools"
    (component / "const.py").write_text("OTHER_VERSION = '8.7.0'\n")
    with pytest.raises(ValueError, match="exactly once"):
        release_builder.stamp_component(
            component, "9.0.0.dev1", "ha-mcp==9.0.0.dev1", "julienld/ha-mcp-native"
        )


def test_hacs_snapshot_selects_exact_zip_without_changing_source_or_workflows(
    source, tmp_path, release_builder
):
    before = {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    output = tmp_path / "distribution"
    proof = release_builder.build(
        source,
        output,
        "9.0.0.dev1",
        "9.0.0.dev1",
        "julienld/ha-mcp-native",
        prepare_hacs=True,
    )
    snapshot = output / "hacs-repository"
    metadata = json.loads((snapshot / "hacs.json").read_text())
    assert metadata["zip_release"] and metadata["filename"] == "ha_mcp_tools.zip"
    assert metadata["hide_default_branch"] and metadata["homeassistant"] == "2026.10.2"
    with zipfile.ZipFile(output / metadata["filename"]) as archive:
        folder = snapshot / "custom_components/ha_mcp_tools"
        assert set(archive.namelist()) == {
            p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()
        }
        assert all(
            archive.read(name) == (folder / name).read_bytes()
            for name in archive.namelist()
        )
    assert proof["revision"] == "a" * 40
    assert proof["hacs_snapshot_files"]["hacs.json"]
    assert not (snapshot / ".github").exists()
    assert {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    } == before


def test_hacs_snapshot_requires_version_tag_before_creating_output(
    source, tmp_path, release_builder
):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError, match="tag must match"):
        release_builder.build(
            source,
            output,
            "9.0.0.dev1",
            "native-ci-123",
            "julienld/ha-mcp-native",
            prepare_hacs=True,
        )
    assert not output.exists()


def test_archive_bytes_do_not_depend_on_staging_file_mtimes(
    source, tmp_path, release_builder
):
    component = source / "custom_components/ha_mcp_tools"
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    release_builder.write_component_archive(component, source / "LICENSE", one)
    for path in source.rglob("*"):
        if path.is_file():
            os.utime(path, (1900000000, 1900000000))
    release_builder.write_component_archive(component, source / "LICENSE", two)
    assert one.read_bytes() == two.read_bytes()


def test_wheel_filename_cannot_conceal_a_different_distribution(
    release_builder, tmp_path
):
    path = tmp_path / "ha_mcp-9.0.0.dev1-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ha_mcp-8.7.0.dist-info/METADATA", "Name: ha-mcp\nVersion: 8.7.0\n"
        )
    with pytest.raises(ValueError, match="metadata does not match"):
        release_builder.validate_wheel_metadata(path, "9.0.0.dev1")
