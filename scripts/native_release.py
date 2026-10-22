# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Build a HACS archive and a matching library wheel from one Git revision."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
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
    manifest_path.write_text(
        json.dumps(manifest, indent=4) + "\n", encoding="utf-8", newline="\n"
    )
    constants = component / "const.py"
    constants.write_text(
        re.sub(
            r'^COMPONENT_VERSION = "[^"]+"$',
            f'COMPONENT_VERSION = "{version}"',
            constants.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
        newline="\n",
    )


def build(repository: Path, output: Path, version: str, tag: str, github: str) -> dict:
    """Stage tracked sources, leaving the working tree and user data untouched."""
    validate_options(version, tag, github)
    revision = run("git", "rev-parse", "HEAD", cwd=repository)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    wheel = output / f"ha_mcp-{version}-py3-none-any.whl"
    dependency = (
        f"ha-mcp @ https://github.com/{github}/releases/download/{tag}/{wheel.name}"
    )
    component_version = version.split(".dev", 1)[0]
    with tempfile.TemporaryDirectory(prefix="ha-mcp-native-release-") as temporary:
        staging = Path(temporary)
        gitlinks = stage_revision(repository, staging, revision)
        stamp_library(staging / "pyproject.toml", version)
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(output)],
            cwd=staging,
            check=True,
        )
        if not wheel.is_file():
            raise RuntimeError(f"Expected library wheel missing: {wheel.name}")
        component = staging / "custom_components" / "ha_mcp_tools"
        stamp_component(component, component_version, dependency, github)
        # HACS extracts this archive directly into custom_components/ha_mcp_tools.
        component_zip = output / "ha_mcp_tools.zip"
        with zipfile.ZipFile(component_zip, "w", zipfile.ZIP_DEFLATED) as bundled:
            for path in sorted(component.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    bundled.write(path, path.relative_to(component).as_posix())
            bundled.write(staging / "LICENSE", "LICENSE")
    evidence = {
        "repository": github,
        "revision": revision,
        "tag": tag,
        "library_version": version,
        "component_version": component_version,
        "dependency": dependency,
        "submodules": gitlinks,
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (wheel, component_zip)
        },
    }
    (output / "native-build.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--github", default="julienld/ha-mcp-native")
    parser.add_argument("--output", type=Path, default=Path("dist/native"))
    options = parser.parse_args()
    result = build(
        Path(__file__).resolve().parents[1],
        options.output,
        options.version,
        options.tag,
        options.github,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
