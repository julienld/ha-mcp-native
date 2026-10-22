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
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

MAX_WHEEL_INDEX_FILES = 10_000
MAX_WHEEL_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_WHEEL_INDEX_BYTES = 4 * 1024 * 1024
_INSTALLER_FILES = frozenset(
    {"INSTALLER", "direct_url.json", "REQUESTED", "uv_cache.json", "uv_build.json"}
)
_CORE_PROVIDED = frozenset(
    {
        "anyio",
        "httpx",
        "cryptography",
        "pyjwt",
        "pyyaml",
        "typing-extensions",
        "psutil",
        "packaging",
        "tzdata",
    }
)
_STANDALONE_ONLY = frozenset(
    {
        "rich",
        "cyclopts",
        "pyperclip",
        "watchfiles",
        "pywin32",
        "authlib",
        "joserfc",
        "jsonschema-path",
        "openapi-pydantic",
    }
)
_KNOWN_NATIVE_EXTRAS = {
    "httpx": {"socks"},
    "pydantic": {"email"},
    "pyjwt": {"crypto"},
    "py-key-value-aio": {"filetree", "keyring", "memory"},
}
_EXTRA_LEAVES = {
    ("httpx", "socks"): "socksio==1.*",
    ("pydantic", "email"): "email-validator>=2.0.0",
    ("py-key-value-aio", "memory"): "cachetools>=5.0.0",
}


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


def _native_requirement(requirement: str) -> list[str]:
    parsed = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[([^\]]+)\])?(.*)", requirement)
    if parsed is None:
        raise ValueError(f"Unrecognized project requirement: {requirement!r}")
    name = re.sub(r"[-_.]+", "-", parsed[1]).lower()
    if name in _STANDALONE_ONLY:
        return []
    extras = {extra.strip() for extra in (parsed[2] or "").split(",") if extra.strip()}
    if extras - _KNOWN_NATIVE_EXTRAS.get(name, set()):
        raise ValueError(f"Native profile needs an explicit extra audit: {requirement}")
    result = [] if name in _CORE_PROVIDED else [parsed[1] + parsed[3]]
    result.extend(
        _EXTRA_LEAVES[(name, extra)]
        for extra in sorted(extras)
        if (name, extra) in _EXTRA_LEAVES
    )
    return result


def select_native_dependencies(project: Path) -> list[str]:
    """Let Core preflight each requirement, without a URL wheel's eager graph."""
    source = project.read_text(encoding="utf-8")
    metadata = tomllib.loads(source)
    original = metadata["project"]["dependencies"]
    native = list(
        dict.fromkeys(
            item
            for requirement in original
            for item in _native_requirement(requirement)
        )
    )
    source, count = re.subn(
        r"(?ms)^dependencies = \[.*?^\]", "dependencies = []", source, count=1
    )
    if count != 1 or "[project.optional-dependencies]" in source:
        raise ValueError("Native dependency profile needs an explicit update")
    source = source.replace(
        "[project.scripts]",
        "[project.optional-dependencies]\nstandalone = "
        + json.dumps(original)
        + "\n\n[project.scripts]",
        1,
    )
    project.write_text(source, encoding="utf-8", newline="\n")
    return native


def stamp_component(
    component: Path,
    version: str,
    dependency: str,
    github: str,
    *,
    native_requirements: list[str] | None = None,
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
        if re.match(r"mcp(?:[<>= @]|$)", requirement) is None
    ]
    manifest["requirements"] = list(
        dict.fromkeys([*manifest["requirements"], *(native_requirements or [])])
    )
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
        native_requirements = select_native_dependencies(staging / "pyproject.toml")
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
        stamp_component(
            component,
            component_version,
            dependency,
            github,
            native_requirements=native_requirements,
        )
        write_wheel_index(wheel, component / "native-wheel.json", version)
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
        "dependency_profile": "native wheel without active dependency edges; individual Core-managed requirements",
        "native_requirements": native_requirements,
        "core_provided": sorted(_CORE_PROVIDED),
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


def _indexed_member(info: zipfile.ZipInfo, dist_info: str) -> str | None:
    name = info.orig_filename
    path = PurePosixPath(name)
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError("A payload index cannot contain symbolic links")
    if info.is_dir():
        return None
    if (
        name != info.filename
        or "\\" in name
        or ":" in name
        or str(path) != name
        or path.is_absolute()
        or ".." in path.parts
        or not name.startswith(("ha_mcp/", dist_info + "/"))
    ):
        raise ValueError(f"Unexpected wheel member path: {name!r}")
    if path.name in _INSTALLER_FILES:
        raise ValueError(f"Installer-owned file must not be shipped: {name!r}")
    if "__pycache__" in path.parts or path.suffix == ".pyc":
        raise ValueError(f"Generated bytecode must not be shipped: {name!r}")
    return None if name == dist_info + "/RECORD" else name


def write_wheel_index(wheel: Path, destination: Path, version: str) -> None:
    """Bind immutable wheel members; uv's installed RECORD is not trusted here."""
    files: dict[str, dict[str, int | str]] = {}
    dist_info = f"ha_mcp-{version}.dist-info"
    total = 0
    seen: set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if info.filename in seen:
                raise ValueError("Duplicate wheel member")
            seen.add(info.filename)
            if (name := _indexed_member(info, dist_info)) is None:
                continue
            total += info.file_size
            if (
                len(files) >= MAX_WHEEL_INDEX_FILES
                or info.file_size < 0
                or total > MAX_WHEEL_PAYLOAD_BYTES
            ):
                raise ValueError("Wheel payload exceeds the native index bounds")
            data = archive.read(info)
            files[name] = {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
    required = {"ha_mcp/__init__.py", dist_info + "/METADATA", dist_info + "/WHEEL"}
    if not required <= files.keys():
        raise ValueError("Wheel index lacks its required package or metadata members")
    payload = (
        json.dumps(
            {
                "format_version": 1,
                "distribution": "ha-mcp",
                "version": version,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "files": files,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_WHEEL_INDEX_BYTES:
        raise ValueError("Wheel index JSON exceeds its native byte bound")
    destination.write_bytes(payload)


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
