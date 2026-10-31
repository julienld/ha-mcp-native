"""Candidate archives must select and identify the exact paired distribution."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version


@pytest.fixture
def release_builder(monkeypatch):
    path = Path(__file__).resolve().parents[3] / "scripts/native_release.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("native_release_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.core_window,
        "resolve_window",
        lambda: module.core_window.make_window(
            "2026.10.4", {"2026.10.0": "3.14.9", "2026.10.4": "3.14.9"}
        ),
    )
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
            {"name": "HA-MCP", "homeassistant": "2026.10.0", "render_readme": True}
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
        'COMPONENT_VERSION = "8.7.0"\nMIN_NATIVE_HOME_ASSISTANT_VERSION = "2026.10.0"\nMIN_EMBEDDED_HOME_ASSISTANT_VERSION = "2026.10.0"\n'
    )
    (component / "hacs.json").write_text(json.dumps({"homeassistant": "2026.10.0"}))
    (component / "__init__.py").write_text("# fixture component\n")
    monkeypatch.setattr(release_builder, "run", lambda *args, **kwargs: "a" * 40)

    def stage(repository, staging, revision):
        shutil.copytree(repository, staging, dirs_exist_ok=True)
        return {"src/ha_mcp/skills/vendor": "b" * 40}

    def wheel_build(args, *, cwd, **kwargs):
        output = Path(args[args.index("--out-dir") + 1])
        output.mkdir(parents=True, exist_ok=True)
        project = tomllib.loads((cwd / "pyproject.toml").read_text())["project"]
        version, name = project["version"], project["name"]
        basename = f"{name.replace('-', '_')}-{version}"
        dependencies = [f"Requires-Dist: {item}\n" for item in project["dependencies"]]
        dependencies.extend(
            f"Requires-Dist: {item}; extra == '{extra}'\n"
            for extra, requirements in project.get("optional-dependencies", {}).items()
            for item in requirements
        )
        with zipfile.ZipFile(output / f"{basename}-py3-none-any.whl", "w") as wheel:
            wheel.writestr("ha_mcp/__init__.py", "# fixture library\n")
            wheel.writestr(
                f"{basename}.dist-info/METADATA",
                f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
                + "".join(dependencies),
            )
            wheel.writestr(
                f"{basename}.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(f"{basename}.dist-info/RECORD", "installer rewrites this\n")
            wheel.writestr(
                f"{basename}.dist-info/entry_points.txt",
                "[console_scripts]\n"
                + "\n".join(
                    f"{key} = {value}" for key, value in project["scripts"].items()
                ),
            )
        if "--wheel" not in args:
            with tarfile.open(output / f"{basename}.tar.gz", "w:gz") as archive:
                payload = (cwd / "pyproject.toml").read_bytes()
                member = tarfile.TarInfo(f"{basename}/pyproject.toml")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
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


def test_release_refuses_stale_floors_even_when_building_old_revision(
    source, tmp_path, release_builder, monkeypatch
):
    monkeypatch.setattr(
        release_builder.core_window,
        "resolve_window",
        lambda: release_builder.core_window.make_window(
            "2026.12.1", {"2026.11.0": "3.15.1", "2026.12.1": "3.15.2"}
        ),
    )
    with pytest.raises(ValueError, match=r"requires minimum 2026\.11\.0"):
        release_builder.build(
            source,
            tmp_path / "rejected",
            "9.0.0",
            "v9.0.0",
            "example/repo",
            revision="old-sha",
            prepare_hacs=True,
        )
    assert not list((tmp_path / "rejected").glob("*.whl"))


def test_qualification_can_measure_new_core_without_publishing_a_stale_floor(
    source, tmp_path, release_builder
):
    window = release_builder.core_window.make_window(
        "2026.12.1", {"2026.11.0": "3.15.1", "2026.12.1": "3.15.2"}
    )
    proof = release_builder.build(
        source,
        tmp_path / "qualification",
        "9.0.0.dev1",
        "native-ci-123",
        "example/repo",
        window=window,
        qualification_only=True,
    )
    assert proof["qualification_only"] and proof["core_window"] == window
    assert proof["hacs_snapshot"] is None
    for tag, prepare in [("v9.0.0", False), ("native-ci-123", True)]:
        with pytest.raises(ValueError, match="Qualification-only"):
            release_builder.build(
                source,
                tmp_path / "not-created",
                "9.0.0",
                tag,
                "example/repo",
                window=window,
                qualification_only=True,
                prepare_hacs=prepare,
            )


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


def test_component_payload_index_binds_installed_bytes_without_mutable_RECORD(
    source, tmp_path, release_builder
):
    import hashlib

    output = tmp_path / "indexed"
    proof = release_builder.build(
        source, output, "9.0.0.dev1", "9.0.0.dev1", "julienld/ha-mcp-native"
    )
    with zipfile.ZipFile(output / "ha_mcp_tools.zip") as archive:
        index = json.loads(archive.read("native-wheel.json"))
    assert index["format_version"] == 1 and index["distribution"] == "ha-mcp"
    assert index["version"] == "9.0.0.dev1"
    assert index["wheel_sha256"] == proof["files"]["ha_mcp-9.0.0.dev1-py3-none-any.whl"]
    assert not any(path.endswith("/RECORD") for path in index["files"])
    assert (
        "ha_mcp/__init__.py" in index["files"]
        and "ha_mcp-9.0.0.dev1.dist-info/WHEEL" in index["files"]
    )
    with zipfile.ZipFile(output / "ha_mcp-9.0.0.dev1-py3-none-any.whl") as wheel:
        for name, expected in index["files"].items():
            data = wheel.read(name)
            assert expected == {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }


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
    assert metadata["hide_default_branch"] and metadata["homeassistant"] == "2026.10.0"
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


@pytest.mark.parametrize(
    "version,tag", [("9.0.0", "v9.0.0"), ("9.0.0.dev123", "v9.0.0-dev.123")]
)
def test_native_profile_accepts_canonical_release_versions_without_recomputing_them(
    source, tmp_path, release_builder, version, tag
):
    output = tmp_path / "release"
    proof = release_builder.build(
        source, output, version, tag, "julienld/ha-mcp-native", prepare_hacs=True
    )
    assert proof["library_version"] == proof["component_version"] == version
    assert proof["tag"] == tag
    assert (output / f"ha_mcp-{version}-py3-none-any.whl").is_file()
    if ".dev" not in version:
        assert "pre-release" not in (output / "hacs-repository/README.md").read_text()


@pytest.mark.parametrize("version", ["9.0.0", "9.0.0.dev123"])
def test_coupled_profiles_keep_cli_graph_and_native_wheel_separate(
    source, tmp_path, release_builder, version, monkeypatch
):
    from unittest.mock import Mock

    stage = Mock(wraps=release_builder.stage_revision)
    monkeypatch.setattr(release_builder, "stage_revision", stage)
    output = tmp_path / "coupled"
    original = (source / "pyproject.toml").read_bytes()
    proof = release_builder.build(
        source,
        output,
        version,
        f"v{version}",
        "julienld/ha-mcp-native",
        with_standalone=True,
        prepare_hacs=True,
        source_github="homeassistant-ai/ha-mcp",
    )
    stage.assert_called_once()
    assert (source / "pyproject.toml").read_bytes() == original
    standalone = proof["standalone"]
    assert standalone["source_revision"] == proof["revision"] == "a" * 40
    assert standalone["version"] == proof["library_version"] == version
    assert proof["source_repository"] == "homeassistant-ai/ha-mcp"
    name = "ha-mcp-dev" if ".dev" in version else "ha-mcp"
    assert standalone["distribution"] == name
    basename = f"{name.replace('-', '_')}-{version}"
    wheel = output / "standalone" / f"{basename}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(
            archive.read(f"{basename}.dist-info/METADATA")
        )
        assert {str(Requirement(r)) for r in metadata.get_all("Requires-Dist")} == {
            str(Requirement(r))
            for r in tomllib.loads(original.decode())["project"]["dependencies"]
        }
        scripts = archive.read(f"{basename}.dist-info/entry_points.txt").decode()
        assert ("ha-mcp-dev = ha_mcp.__main__:main_dev" in scripts) == (
            ".dev" in version
        )
    with tarfile.open(output / "standalone" / f"{basename}.tar.gz") as archive:
        project = tomllib.loads(
            archive.extractfile(f"{basename}/pyproject.toml").read().decode()
        )["project"]
        assert project["dependencies"] == standalone["dependencies"]
        assert project["name"] == name and project["version"] == version
    with zipfile.ZipFile(output / f"ha_mcp-{version}-py3-none-any.whl") as archive:
        metadata = BytesParser().parsebytes(
            archive.read(f"ha_mcp-{version}.dist-info/METADATA")
        )
        assert not any(
            requirement.marker is None or requirement.marker.evaluate({"extra": ""})
            for value in metadata.get_all("Requires-Dist")
            for requirement in [Requirement(value)]
        )
    assert set(standalone["files"]) == {
        f"standalone/{basename}-py3-none-any.whl",
        f"standalone/{basename}.tar.gz",
    }
    assert not (set(standalone["files"]) & set(proof["files"]))
    provenance = json.loads((output / "hacs-repository/native-source.json").read_text())
    assert (
        provenance["source_url"]
        == f"https://github.com/homeassistant-ai/ha-mcp/tree/{'a' * 40}"
    )
    assert provenance["release_tag"] == f"v{version}"
    assert (
        "distribution_revision" not in provenance
    )  # No distribution commit exists yet.
