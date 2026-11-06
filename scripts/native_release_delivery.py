# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Publish paired native assets once, or transfer their exact bytes to a mirror."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Literal, overload
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import native_published_assets as assets


def gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def api(endpoint: str, *, missing: bool = False) -> Any:
    result = subprocess.run(
        ["gh", "api", endpoint], capture_output=True, text=True, check=False
    )
    if result.returncode:
        # Authentication, rate limits and transport errors must not create a release.
        if missing and re.search(r"\(HTTP 404\)", result.stderr):
            return None
        raise RuntimeError(f"GitHub API request failed: {endpoint}")
    return json.loads(result.stdout)


def download_asset(repository: str, asset: dict, destination: Path) -> None:
    with destination.open("xb") as stream:
        subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/releases/assets/{asset['id']}",
                "-H",
                "Accept: application/octet-stream",
            ],
            stdout=stream,
            check=True,
        )


@overload
def release_by_tag(
    repository: str, tag: str, *, missing: Literal[False] = False
) -> dict:
    pass


@overload
def release_by_tag(repository: str, tag: str, *, missing: Literal[True]) -> dict | None:
    pass


def release_by_tag(repository: str, tag: str, *, missing: bool = False) -> dict | None:
    """Find a unique release, including pending drafts visible to the publisher."""
    pages = json.loads(
        gh("api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100")
    )
    if not isinstance(pages, list) or not pages:
        raise ValueError("GitHub returned an invalid release listing")
    matches = []
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("GitHub returned an invalid release page")
        for release in page:
            if not isinstance(release, dict) or not isinstance(
                release.get("tag_name"), str
            ):
                raise ValueError("GitHub returned an invalid release object")
            if release["tag_name"] == tag:
                matches.append(release)
    if len(matches) > 1:
        raise ValueError(f"Multiple releases identify {repository}@{tag}")
    if not matches:
        if missing:
            return None
        raise RuntimeError(f"GitHub release not found: {repository}@{tag}")
    return validate_release(matches[0], tag)


def validate_release(release: Any, tag: str) -> dict:
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        raise ValueError("GitHub returned an invalid release identity")
    if type(release.get("id")) is not int or release["id"] <= 0:
        raise ValueError("GitHub returned an invalid release ID")
    if (
        type(release.get("draft")) is not bool
        or type(release.get("prerelease")) is not bool
        or not isinstance(release.get("assets"), list)
    ):
        raise ValueError("GitHub returned an invalid release object")
    return release


def confirm_release(
    repository: str, tag: str, release_id: int, prerelease: bool
) -> dict:
    release = release_by_tag(repository, tag)
    if release["id"] != release_id:
        raise ValueError("Selected release identity changed")
    if release["prerelease"] != prerelease:
        raise ValueError("Existing release channel disagrees")
    return release


def create_draft(
    repository: str, tag: str, revision: str, notes: Path, prerelease: bool
) -> dict:
    created = validate_release(
        json.loads(
            gh(
                "api",
                f"repos/{repository}/releases",
                "--method",
                "POST",
                "-f",
                f"tag_name={tag}",
                "-f",
                f"target_commitish={revision}",
                "-f",
                f"name={tag}",
                "-F",
                f"body=@{notes}",
                "-F",
                "draft=true",
                "-F",
                f"prerelease={str(prerelease).lower()}",
            )
        ),
        tag,
    )
    if not created["draft"] or created["prerelease"] != prerelease:
        raise ValueError("Created release is not a draft in the requested channel")
    release = confirm_release(repository, tag, created["id"], prerelease)
    if not release["draft"]:
        raise ValueError("Created release is no longer a draft")
    return release


def upload_asset(repository: str, release: dict, path: Path) -> None:
    upload_url = release.get("upload_url")
    expected = (
        f"https://uploads.github.com/repos/{repository}/releases/{release['id']}/assets"
    )
    if (
        not isinstance(upload_url, str)
        or upload_url.removesuffix("{?name,label}") != expected
    ):
        raise ValueError("GitHub returned an invalid release upload URL")
    gh(
        "api",
        upload_url.removesuffix("{?name,label}") + "?" + urlencode({"name": path.name}),
        "--method",
        "POST",
        "-H",
        "Content-Type: application/octet-stream",
        "--input",
        str(path),
    )


def publish_draft(repository: str, release: dict) -> None:
    published = validate_release(
        json.loads(
            gh(
                "api",
                f"repos/{repository}/releases/{release['id']}",
                "--method",
                "PATCH",
                "-F",
                "draft=false",
            )
        ),
        release["tag_name"],
    )
    if (published["id"], published["draft"], published["prerelease"]) != (
        release["id"],
        False,
        release["prerelease"],
    ):
        raise ValueError("Published release identity or channel disagrees")


def verify_bundle(directory: Path, repository: str, tag: str, revision: str) -> dict:
    proof = json.loads((directory / "native-build.json").read_bytes())
    if not isinstance(proof, dict):
        raise ValueError("Release provenance must be a JSON object")
    contract = assets.release_contract(proof, tag, revision, repository=repository)
    if proof.get("qualification_only") is not False:
        raise ValueError("Qualification-only or unclassified builds cannot be released")
    if proof.get("source_repository") != repository:
        raise ValueError("Canonical release source repository disagrees")
    if tag != f"v{contract['version']}":
        raise ValueError(
            "Canonical release tag must identify the exact library version"
        )
    wheel = directory / contract["wheel"]
    assets.verify_wheel(wheel, proof, contract)
    assets.verify_component(directory / "ha_mcp_tools.zip", proof, contract)
    verify_payload(directory, proof)
    return proof


def verify_payload(directory: Path, proof: dict) -> None:
    version = proof["library_version"]
    dist_info = f"ha_mcp-{version}.dist-info"
    with (
        zipfile.ZipFile(directory / "ha_mcp_tools.zip") as component,
        zipfile.ZipFile(directory / f"ha_mcp-{version}-py3-none-any.whl") as wheel,
    ):
        index = json.loads(component.read("native-wheel.json"))
        if (
            index.get("format_version"),
            index.get("distribution"),
            index.get("version"),
            index.get("wheel_sha256"),
        ) != (
            1,
            "ha-mcp",
            version,
            proof["files"][f"ha_mcp-{version}-py3-none-any.whl"],
        ):
            raise ValueError("Component wheel index identifies another build")
        expected = {}
        for name in wheel.namelist():
            if name.endswith("/") or name == dist_info + "/RECORD":
                continue
            if name in expected:
                raise ValueError("Duplicate wheel payload member")
            data = wheel.read(name)
            expected[name] = {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        if index.get("files") != expected:
            raise ValueError("Component wheel index disagrees with payload bytes")
        metadata = BytesParser().parsebytes(wheel.read(dist_info + "/METADATA"))
        for requirement in metadata.get_all("Requires-Dist", []):
            marker = requirement.partition(";")[2].strip()
            if " or " in marker or not re.search(
                r"(?:^| and )extra == ['\"]standalone['\"]$", marker
            ):
                raise ValueError("Native wheel must not have active dependency edges")
        constants = ast.parse(component.read("const.py").decode())
        versions = [
            ast.literal_eval(node.value)
            for node in constants.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "COMPONENT_VERSION"
                for target in node.targets
            )
        ]
        if versions != [proof["component_protocol_version"]]:
            raise ValueError("Component protocol version disagrees with provenance")


def verify_recorded(directory: Path, recorded: Path) -> None:
    if (directory / "native-build.json").read_bytes() != recorded.read_bytes():
        raise ValueError(
            "Upstream release differs from the mirror's recorded provenance"
        )


def release_revision(proof: dict) -> str:
    revision = proof.get("release_revision", proof["revision"])
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Invalid release source stamp revision")
    return revision


def stage_dev_source(checkout: Path, directory: Path) -> None:
    proof = json.loads((directory / "native-build.json").read_bytes())
    verify_bundle(directory, proof["repository"], proof["tag"], proof["revision"])
    if ".dev" not in proof["library_version"]:
        raise ValueError("Only development tags need a separate source stamp")
    with zipfile.ZipFile(directory / "ha_mcp_tools.zip") as archive:
        for name in ("manifest.json", "const.py"):
            (checkout / "custom_components" / "ha_mcp_tools" / name).write_bytes(
                archive.read(name)
            )


def git_bytes(checkout: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=checkout, check=True, capture_output=True
    ).stdout


def attest_dev_source(checkout: Path, directory: Path, revision: str) -> None:
    proof_path = directory / "native-build.json"
    proof = json.loads(proof_path.read_bytes())
    source = proof["revision"]
    if (
        ".dev" not in proof["library_version"]
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
    ):
        raise ValueError("Expected a development source stamp revision")
    parents = (
        git_bytes(checkout, "show", "-s", "--format=%P", revision).decode().strip()
    )
    paths = {
        f"custom_components/ha_mcp_tools/{name}"
        for name in ("manifest.json", "const.py")
    }
    changed = set(
        git_bytes(checkout, "diff", "--name-only", source, revision)
        .decode()
        .splitlines()
    )
    if parents != source or changed != paths:
        raise ValueError(
            "Dev source stamp must change only paired metadata on the built source parent"
        )
    with zipfile.ZipFile(directory / "ha_mcp_tools.zip") as archive:
        for path in paths:
            if git_bytes(checkout, "show", f"{revision}:{path}") != archive.read(
                Path(path).name
            ):
                raise ValueError(
                    "Tagged source metadata differs from the installable archive"
                )
    if proof.get("release_revision", revision) != revision:
        raise ValueError("Dev source stamp differs from the original release revision")
    proof["release_revision"] = revision
    proof_path.write_text(
        json.dumps(proof, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def fetch_recorded(recorded: Path, output: Path) -> None:
    proof = json.loads(recorded.read_bytes())
    repository, tag, revision = proof["repository"], proof["tag"], proof["revision"]
    contract = assets.release_contract(proof, tag, revision, repository=repository)
    release = release_by_tag(repository, tag)
    if release["draft"] or api(f"repos/{repository}/commits/{tag}")[
        "sha"
    ] != release_revision(proof):
        raise ValueError(
            "Mirror requires a public release at the recorded source revision"
        )
    names = {"native-build.json", "ha_mcp_tools.zip", contract["wheel"]}
    remote = release_assets(release)
    if not names <= remote.keys():
        raise ValueError("Upstream release is missing paired assets")
    output.mkdir(parents=True, exist_ok=False)
    for name in sorted(names):
        download_asset(repository, remote[name], output / name)
    verify_recorded(output, recorded)
    verify_bundle(output, repository, tag, revision)


def stage_mirror(
    directory: Path, mirror: Path, repository: str, tag: str, revision: str
) -> None:
    verify_bundle(directory, repository, tag, revision)
    component = mirror / "custom_components" / "ha_mcp_tools"
    if not component.resolve().is_relative_to(mirror.resolve()):
        raise ValueError("Component destination escapes the mirror")
    if component.exists():
        # This destination is fixed under the explicitly supplied mirror checkout.
        shutil.rmtree(component)
    with zipfile.ZipFile(directory / "ha_mcp_tools.zip") as archive:
        archive.extractall(component)
    shutil.copyfile(directory / "native-build.json", mirror / "native-build.json")


def build_bundle(
    checkout: Path, output: Path, version: str, tag: str, repository: str, revision: str
) -> None:
    from scripts import native_release

    native_release.build(checkout, output, version, tag, repository, revision=revision)


def release_assets(release: dict) -> dict[str, dict]:
    result = {item["name"]: item for item in release["assets"]}
    if len(result) != len(release["assets"]):
        raise ValueError("Duplicate release asset names")
    return result


def prepare(
    checkout: Path, output: Path, version: str, tag: str, repository: str, revision: str
) -> None:
    assets.validate_repository(repository)
    assets.validate_tag(tag)
    release = release_by_tag(repository, tag, missing=True)
    if release is None:
        build_bundle(checkout, output, version, tag, repository, revision)
    else:
        # Reuse the original build, even when today's Core window has moved.
        # Missing assets in a partial draft require an explicit recovery using
        # the original artifact, never a new build under the old identity.
        names = {
            "native-build.json",
            "ha_mcp_tools.zip",
            f"ha_mcp-{version}-py3-none-any.whl",
        }
        remote = release_assets(release)
        if not names <= remote.keys():
            raise ValueError(
                "Incomplete existing release; recover using its original build artifact"
            )
        output.mkdir(parents=True, exist_ok=False)
        for name in sorted(names):
            download_asset(repository, remote[name], output / name)
    proof = verify_bundle(output, repository, tag, revision)
    if proof["library_version"] != version:
        raise ValueError("Recovered release version disagrees")


def check_remote_assets(
    directory: Path, repository: str, release: dict, names: set[str]
) -> list[str]:
    remote = release_assets(release)
    missing = sorted(names - remote.keys())
    if missing and not release["draft"]:
        raise ValueError(
            "Published release has missing native assets; refusing mutation"
        )
    with tempfile.TemporaryDirectory(prefix="ha-mcp-release-verify-") as temporary:
        for name in sorted(names & remote.keys()):
            downloaded = Path(temporary) / name
            download_asset(repository, remote[name], downloaded)
            if assets.digest(downloaded) != assets.digest(directory / name):
                raise ValueError(f"Existing release asset differs: {name}")
    return missing


def publish(
    directory: Path,
    repository: str,
    tag: str,
    revision: str,
    notes: Path,
    *,
    draft_only: bool = False,
    recorded: Path | None = None,
) -> None:
    proof = json.loads((directory / "native-build.json").read_bytes())
    verify_bundle(directory, proof["repository"], proof["tag"], proof["revision"])
    assets.validate_repository(repository)
    assets.validate_tag(tag)
    if recorded is None:
        if (repository, tag, revision) != (
            proof["repository"],
            proof["tag"],
            release_revision(proof),
        ):
            raise ValueError("Publisher and bundle identity disagree")
    else:
        verify_recorded(directory, recorded)
        expected_tag = "v" + proof["library_version"].replace(".dev", "-dev.")
        if tag != expected_tag:
            raise ValueError("Mirror tag does not identify the recorded build")
    commit = api(f"repos/{repository}/commits/{tag}")
    if commit["sha"] != revision:
        raise ValueError("Remote release tag points to another source revision")
    release = release_by_tag(repository, tag, missing=True)
    prerelease = ".dev" in proof["library_version"]
    if release is None:
        release = create_draft(repository, tag, revision, notes, prerelease)
    if release["prerelease"] != prerelease:
        raise ValueError("Existing release channel disagrees")
    names = {*proof["files"], "native-build.json"}
    release_id = release["id"]
    for name in check_remote_assets(directory, repository, release, names):
        upload_asset(repository, release, directory / name)
    release = confirm_release(repository, tag, release_id, prerelease)
    if check_remote_assets(directory, repository, release, names):
        raise ValueError("Release upload remains incomplete")
    if release["draft"] and not draft_only:
        publish_draft(repository, release)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "publish",
            "stage-mirror",
            "verify",
            "fetch-recorded",
            "stage-dev-source",
            "attest-dev-source",
        ],
    )
    parser.add_argument("--repository")
    parser.add_argument("--tag")
    parser.add_argument("--revision")
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--version")
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--recorded", type=Path)
    parser.add_argument("--draft-only", action="store_true")
    options = parser.parse_args()
    required = {
        "prepare": "version",
        "publish": "notes",
        "stage-mirror": "mirror",
        "fetch-recorded": "recorded",
    }
    if (key := required.get(options.command)) and not getattr(options, key):
        parser.error(f"{options.command} requires --{key}")
    if options.command != "fetch-recorded" and not all(
        (options.repository, options.tag, options.revision)
    ):
        parser.error("this command requires --repository, --tag and --revision")
    return options


def main() -> None:
    options = arguments()
    if options.command == "fetch-recorded":
        fetch_recorded(options.recorded, options.assets)
        return
    if options.command == "stage-dev-source":
        stage_dev_source(options.checkout, options.assets)
    elif options.command == "attest-dev-source":
        attest_dev_source(options.checkout, options.assets, options.revision)
    elif options.command == "prepare":
        prepare(
            options.checkout,
            options.assets,
            options.version,
            options.tag,
            options.repository,
            options.revision,
        )
    elif options.command == "publish":
        publish(
            options.assets,
            options.repository,
            options.tag,
            options.revision,
            options.notes,
            draft_only=options.draft_only,
            recorded=options.recorded,
        )
    elif options.command == "stage-mirror":
        stage_mirror(
            options.assets,
            options.mirror,
            options.repository,
            options.tag,
            options.revision,
        )
    else:
        verify_bundle(options.assets, options.repository, options.tag, options.revision)
        if options.recorded:
            verify_recorded(options.assets, options.recorded)


if __name__ == "__main__":
    main()
