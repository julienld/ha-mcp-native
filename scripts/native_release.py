# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Build a HACS archive and a matching library wheel from one Git revision."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def validate_options(version: str, tag: str, github: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+\.dev\d+", version):
        raise ValueError("Use an explicit X.Y.Z.devN development library version")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("Invalid release tag")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github):
        raise ValueError("Expected owner/repository")


def stage_revision(repository: Path, staging: Path, revision: str) -> dict[str, str]:
    """Export immutable tracked sources, including the pinned skill submodule."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    gitlinks = {}
    for line in run("git", "ls-tree", "-r", revision, cwd=repository).splitlines():
        metadata, path = line.split("\t", 1)
        mode, _, object_id = metadata.split()
        if mode == "160000":
            gitlinks[path] = object_id
    with tarfile.open(fileobj=io.BytesIO(archive)) as tracked:
        tracked.extractall(staging, filter="data")
    for path, object_id in gitlinks.items():
        submodule_archive = subprocess.run(
            ["git", "archive", "--format=tar", object_id],
            cwd=repository / path,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        destination = staging / path
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(submodule_archive)) as tracked:
            tracked.extractall(destination, filter="data")
    return gitlinks


def stamp_library(project: Path, version: str) -> None:
    source = project.read_text(encoding="utf-8")
    original_version = tomllib.loads(source)["project"]["version"]
    source, replacements = re.subn(
        rf'^version = "{re.escape(original_version)}"$',
        f'version = "{version}"',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if replacements != 1:
        raise ValueError("Project version was not stamped exactly once")
    project.write_text(source, encoding="utf-8", newline="\n")


def select_native_dependencies(project: Path) -> None:
    """Keep CLI-only dependencies out of Home Assistant's shared environment."""
    source = project.read_text(encoding="utf-8")
    metadata = tomllib.loads(source)
    excluded_names = {"rich", "cyclopts", "pyperclip", "watchfiles", "pywin32"}
    native: list[str] = []
    standalone: list[str] = []
    for requirement in metadata["project"]["dependencies"]:
        name = re.match(r"[A-Za-z0-9_.-]+", requirement)
        assert name is not None
        target = (
            standalone
            if name[0].lower().replace("_", "-") in excluded_names
            else native
        )
        target.append(requirement)
    replacement = (
        "dependencies = [\n"
        + "".join(f"    {json.dumps(requirement)},\n" for requirement in native)
        + "]"
    )
    source, count = re.subn(
        r"(?ms)^dependencies = \[.*?^\]", lambda _: replacement, source, count=1
    )
    if count != 1 or "[project.optional-dependencies]" in source:
        raise ValueError("Native dependency profile needs an explicit update")
    source = source.replace(
        "[project.scripts]",
        "[project.optional-dependencies]\nstandalone = "
        + json.dumps(standalone)
        + "\n\n[project.scripts]",
        1,
    )
    project.write_text(source, encoding="utf-8", newline="\n")


def stamp_component(
    component: Path, version: str, dependency: str, github: str
) -> None:
    manifest_path = component / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest["documentation"] = f"https://github.com/{github}"
    manifest["issue_tracker"] = f"https://github.com/{github}/issues"
    manifest["requirements"] = [
        dependency
        if re.match(r"ha-mcp(?:-dev)?(?:[<>= @]|$)", requirement)
        else requirement
        for requirement in manifest["requirements"]
    ]
    if manifest["requirements"].count(dependency) != 1:
        raise ValueError("Expected exactly one library requirement")
    constants = component / "const.py"
    protocol_version = version.split(".dev", 1)[0]
    stamped, count = re.subn(
        r'^COMPONENT_VERSION = "[^"]+"$',
        f'COMPONENT_VERSION = "{protocol_version}"',
        constants.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("Component protocol version was not stamped exactly once")
    manifest_path.write_text(
        json.dumps(manifest, indent=4) + "\n", encoding="utf-8", newline="\n"
    )
    constants.write_text(stamped, encoding="utf-8", newline="\n")


def build(
    repository: Path,
    output: Path,
    version: str,
    tag: str,
    github: str,
    *,
    prepare_hacs: bool = False,
    revision: str = "HEAD",
) -> dict:
    """Stage tracked sources, leaving the working tree and user data untouched."""
    validate_options(version, tag, github)
    builder_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if prepare_hacs and tag.removeprefix("v") != version:
        raise ValueError(
            "A HACS distribution tag must match its X.Y.Z.devN manifest version"
        )
    revision = run(
        "git", "rev-parse", "--verify", f"{revision}^{{commit}}", cwd=repository
    )
    if re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", revision) is None:
        raise ValueError("Could not resolve an immutable Git commit")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    wheel = output / f"ha_mcp-{version}-py3-none-any.whl"
    dependency = (
        f"ha-mcp @ https://github.com/{github}/releases/download/{tag}/{wheel.name}"
    )
    component_version = version
    with tempfile.TemporaryDirectory(prefix="ha-mcp-native-release-") as temporary:
        staging = Path(temporary)
        gitlinks = stage_revision(repository, staging, revision)
        stamp_library(staging / "pyproject.toml", version)
        select_native_dependencies(staging / "pyproject.toml")
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(output)],
            cwd=staging,
            check=True,
        )
        if not wheel.is_file():
            raise RuntimeError(f"Expected library wheel missing: {wheel.name}")
        validate_wheel_metadata(wheel, version)
        dependency += "#sha256=" + hashlib.sha256(wheel.read_bytes()).hexdigest()
        component = staging / "custom_components" / "ha_mcp_tools"
        stamp_component(component, component_version, dependency, github)
        # HACS extracts this archive directly into custom_components/ha_mcp_tools.
        component_zip = output / "ha_mcp_tools.zip"
        write_component_archive(component, staging / "LICENSE", component_zip)
        if prepare_hacs:
            prepare_hacs_snapshot(
                staging, component, output, version, tag, github, revision, dependency
            )
    evidence = {
        "repository": github,
        "revision": revision,
        "builder_sha256": builder_sha256,
        "tag": tag,
        "library_version": version,
        "dependency_profile": "native (standalone launchers are optional)",
        "component_version": component_version,
        "component_protocol_version": version.split(".dev", 1)[0],
        "dependency": dependency,
        "submodules": gitlinks,
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (wheel, component_zip)
        },
        "hacs_snapshot": "hacs-repository" if prepare_hacs else None,
    }
    if prepare_hacs:
        snapshot = output / "hacs-repository"
        evidence["hacs_snapshot_files"] = {
            path.relative_to(snapshot).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(snapshot.rglob("*"))
            if path.is_file()
        }
    (output / "native-build.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return evidence


def validate_wheel_metadata(wheel: Path, version: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        candidates = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(candidates) != 1:
            raise ValueError("Expected one wheel distribution metadata record")
        metadata = BytesParser().parsebytes(archive.read(candidates[0]))
    if metadata["Name"] != "ha-mcp" or metadata["Version"] != version:
        raise ValueError("Wheel metadata does not match the candidate library version")


def write_component_archive(
    component: Path, license_file: Path, destination: Path
) -> None:
    """Stable ZIP bytes for identical component content, independent of mtimes."""
    entries = {
        path.relative_to(component).as_posix(): path
        for path in component.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    entries["LICENSE"] = license_file
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def prepare_hacs_snapshot(
    staging: Path,
    component: Path,
    output: Path,
    version: str,
    tag: str,
    github: str,
    revision: str,
    dependency: str,
) -> None:
    """Prepare a reviewable tag tree; never create a commit, tag or release."""
    destination = output / "hacs-repository"
    shutil.copytree(component, destination / "custom_components" / "ha_mcp_tools")
    shutil.copyfile(staging / "LICENSE", destination / "LICENSE")
    shutil.copyfile(
        staging / "LICENSE",
        destination / "custom_components" / "ha_mcp_tools" / "LICENSE",
    )
    constants = ast.parse((component / "const.py").read_text(encoding="utf-8"))
    minimum = next(
        ast.literal_eval(node.value)
        for node in constants.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "MIN_NATIVE_HOME_ASSISTANT_VERSION"
            for target in node.targets
        )
    )
    metadata = {
        "name": "HA-MCP Native Candidate",
        "homeassistant": minimum,
        "render_readme": True,
        "zip_release": True,
        "filename": "ha_mcp_tools.zip",
        "hide_default_branch": True,
    }
    (destination / "hacs.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    source_url = f"https://github.com/{github}/tree/{revision}"
    (destination / "README.md").write_text(
        f"# HA-MCP native candidate {version}\n\n"
        f"Prepared distribution snapshot from [{revision}]({source_url}).\n\n"
        "This tree becomes installable only after its matching GitHub pre-release and assets are published. "
        f"Select tag `{tag}` with pre-releases enabled in HACS; the default branch is not this candidate.\n\n"
        f"Requires Home Assistant {minimum} or newer. Install the attached `ha_mcp_tools.zip`, then restart Home Assistant. "
        "The library wheel is installed by Home Assistant from the exact URL and SHA-256 in the integration manifest.\n\n"
        f"Read the [installation, permissions and rollback guide](https://github.com/{github}/blob/{revision}/docs/native-migration/installation.md) before switching runtimes. "
        "Existing configuration IDs remain in place. Do not delete the integration as an upgrade step.\n",
        encoding="utf-8",
        newline="\n",
    )
    proof = {
        "source_revision": revision,
        "source_url": source_url,
        "release_tag": tag,
        "library_version": version,
        "component_version": version,
        "component_protocol_version": version.split(".dev", 1)[0],
        "dependency": dependency,
        "component_zip_sha256": hashlib.sha256(
            (output / "ha_mcp_tools.zip").read_bytes()
        ).hexdigest(),
    }
    (destination / "native-source.json").write_text(
        json.dumps(proof, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--github", default="julienld/ha-mcp-native")
    parser.add_argument(
        "--revision",
        default="HEAD",
        help="Committed source to export, resolved once before building",
    )
    parser.add_argument("--output", type=Path, default=Path("dist/native"))
    parser.add_argument(
        "--prepare-hacs",
        action="store_true",
        help="Prepare the separate HACS tag tree without Git or publication actions",
    )
    options = parser.parse_args()
    result = build(
        Path(__file__).resolve().parents[1],
        options.output,
        options.version,
        options.tag,
        options.github,
        prepare_hacs=options.prepare_hacs,
        revision=options.revision,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
