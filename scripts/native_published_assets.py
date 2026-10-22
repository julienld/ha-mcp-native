# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Fetch and verify one public native release for the disposable Core CI lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

REPOSITORY = "julienld/ha-mcp-native"
MAX_JSON = 1024 * 1024
MAX_ZIP = 64 * 1024 * 1024
MAX_WHEEL = 128 * 1024 * 1024
_CDN_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}


def validate_tag(tag: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag) is None:
        raise ValueError("Expected a single release tag without path separators")


def _asset_url(tag: str, filename: str) -> str:
    validate_tag(tag)
    if Path(filename).name != filename or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", filename
    ):
        raise ValueError("Invalid asset basename")
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}"


def _redirect_allowed(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _CDN_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Release download redirected outside GitHub HTTPS asset hosts")


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 1

    def redirect_request(
        self,
        request: Any,
        file: Any,
        code: int,
        message: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        _redirect_allowed(newurl)
        return super().redirect_request(request, file, code, message, headers, newurl)


def download(tag: str, filename: str, destination: Path, maximum: int) -> None:
    request = urllib.request.Request(
        _asset_url(tag, filename),
        headers={
            "User-Agent": "ha-mcp-native-release-qualification",
            "Accept": "application/octet-stream",
        },
    )
    opener = urllib.request.build_opener(AssetRedirect())
    deadline = time.monotonic() + 120
    try:
        with (
            opener.open(request, timeout=30) as response,
            destination.open("xb") as output,
        ):
            _redirect_allowed(response.url)
            if int(response.headers.get("Content-Length", "0")) > maximum:
                raise ValueError("Release asset exceeds its download limit")
            received = 0
            # read1 returns available bytes instead of waiting to fill a chunk;
            # a trickling response must still reach the cumulative time check.
            while chunk := response.read1(64 * 1024):
                received += len(chunk)
                if received > maximum or time.monotonic() > deadline:
                    raise ValueError("Release asset exceeded its size or time limit")
                output.write(chunk)
    except (urllib.error.URLError, TimeoutError) as error:
        # CDN redirects contain signed query parameters. Never print them.
        raise RuntimeError(
            f"Public release asset {filename} is unavailable ({type(error).__name__})"
        ) from None


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def release_contract(
    proof: dict[str, Any],
    tag: str,
    revision: str,
    submodules: dict[str, str] | None = None,
) -> dict[str, str]:
    validate_tag(tag)
    if not isinstance(proof, dict):
        raise ValueError("Expected a release metadata object")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Expected the full checked-out source revision")
    if (proof.get("repository"), proof.get("tag"), proof.get("revision")) != (
        REPOSITORY,
        tag,
        revision,
    ):
        raise ValueError(
            "Published assets do not identify this repository, tag and source checkout"
        )
    version = proof.get("library_version")
    if (
        not isinstance(version, str)
        or re.fullmatch(r"\d+\.\d+\.\d+\.dev\d+", version) is None
    ):
        raise ValueError("Invalid native library version")
    if (
        proof.get("component_version") != version
        or proof.get("component_protocol_version") != version.split(".dev")[0]
    ):
        raise ValueError("Component and wheel version provenance disagree")
    if proof.get("dependency_profile") != "native wheel without active dependency edges; individual Core-managed requirements":
        raise ValueError("Expected the native dependency profile")
    declared = proof.get("submodules")
    if (
        not isinstance(declared, dict)
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None
            for value in declared.values()
        )
        or (submodules is not None and declared != submodules)
    ):
        raise ValueError(
            "Published submodule provenance disagrees with the source checkout"
        )
    wheel = f"ha_mcp-{version}-py3-none-any.whl"
    files = proof.get("files")
    if (
        not isinstance(files, dict)
        or set(files) != {wheel, "ha_mcp_tools.zip"}
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in files.values()
        )
    ):
        raise ValueError("Expected exact wheel and component SHA-256 entries")
    url = _asset_url(tag, wheel)
    dependency = f"ha-mcp @ {url}#sha256={files[wheel]}"
    if proof.get("dependency") != dependency:
        raise ValueError(
            "Published dependency must point to this exact public wheel and digest"
        )
    return {"wheel": wheel, "version": version, "url": url, "dependency": dependency}


def _safe_component_entries(archive: zipfile.ZipFile) -> None:
    names: set[str] = set()
    total = 0
    for entry in archive.infolist():
        # ZipInfo normalizes backslashes on Windows and truncates at NUL. Inspect
        # the original name too, so verification is identical on both platforms.
        original = entry.orig_filename
        path = PurePosixPath(original)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if (
            not original
            or "\x00" in original
            or "\\" in original
            or ":" in original
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() in names
            or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
        ):
            raise ValueError("Unsafe or duplicate component archive entry")
        names.add(path.as_posix())
        total += entry.file_size
        if len(names) > 10_000 or total > 128 * 1024 * 1024:
            raise ValueError("Component archive exceeds extraction bounds")


def verify_component(
    path: Path, proof: dict[str, Any], contract: dict[str, str]
) -> str:
    if path.stat().st_size > MAX_ZIP:
        raise ValueError("Component archive exceeds its download limit")
    if digest(path) != proof["files"]["ha_mcp_tools.zip"]:
        raise ValueError("Component archive SHA-256 mismatch")
    with zipfile.ZipFile(path) as archive:
        _safe_component_entries(archive)
        if archive.getinfo("manifest.json").file_size > MAX_JSON:
            raise ValueError("Component manifest exceeds its bound")
        raw = archive.read("manifest.json")
    manifest = json.loads(raw)
    requirements = manifest.get("requirements", [])
    paired = [
        value
        for value in requirements
        if isinstance(value, str)
        and re.match(r"ha[-_]mcp(?:[-_]dev)?(?:[<>= @]|$)", value, re.IGNORECASE)
    ]
    if (
        manifest.get("domain") != "ha_mcp_tools"
        or manifest.get("version") != contract["version"]
        or paired != [contract["dependency"]]
    ):
        raise ValueError(
            "Archive manifest does not select the verified wheel unchanged"
        )
    return hashlib.sha256(raw).hexdigest()


def verify_wheel(path: Path, proof: dict[str, Any], contract: dict[str, str]) -> None:
    if digest(path) != proof["files"][contract["wheel"]]:
        raise ValueError("Wheel SHA-256 mismatch")
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if len(PurePosixPath(name).parts) == 2
            and name.endswith(".dist-info/METADATA")
        ]
        if len(names) != 1 or archive.getinfo(names[0]).file_size > MAX_JSON:
            raise ValueError("Expected one bounded wheel METADATA record")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if (
        metadata.get("Name") != "ha-mcp"
        or metadata.get("Version") != contract["version"]
    ):
        raise ValueError(
            "Wheel distribution metadata does not match release provenance"
        )


def checkout_provenance(checkout: Path) -> tuple[str, dict[str, str]]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=checkout, check=True, capture_output=True, text=True
        ).stdout.strip()

    revision = git("rev-parse", "HEAD")
    submodules = {}
    for line in git("ls-tree", "-r", revision).splitlines():
        metadata, name = line.split("\t", 1)
        mode, _kind, object_id = metadata.split()
        if mode == "160000":
            submodules[name] = object_id
    return revision, submodules


def fetch(checkout: Path, output: Path, tag: str) -> dict[str, Any]:
    validate_tag(tag)
    revision, submodules = checkout_provenance(checkout)
    output.mkdir(parents=True, exist_ok=False)
    download(tag, "native-build.json", output / "native-build.json", MAX_JSON)
    proof = json.loads((output / "native-build.json").read_bytes())
    contract = release_contract(proof, tag, revision, submodules)
    download(tag, contract["wheel"], output / contract["wheel"], MAX_WHEEL)
    download(tag, "ha_mcp_tools.zip", output / "ha_mcp_tools.zip", MAX_ZIP)
    verify_wheel(output / contract["wheel"], proof, contract)
    manifest_hash = verify_component(output / "ha_mcp_tools.zip", proof, contract)
    verified = {
        "repository": REPOSITORY,
        "source_revision": revision,
        "tag": tag,
        "wheel_url": contract["url"],
        "wheel_sha256": proof["files"][contract["wheel"]],
        "component_sha256": proof["files"]["ha_mcp_tools.zip"],
        "manifest_sha256": manifest_hash,
        "metadata_sha256": digest(output / "native-build.json"),
    }
    (output / "published-verification.json").write_text(
        json.dumps(verified, indent=2) + "\n", encoding="utf-8"
    )
    return verified


def prepare_image(assets: Path, destination: Path, revision: str) -> None:
    if any(
        (assets / name).stat().st_size > MAX_JSON
        for name in ("native-build.json", "published-verification.json")
    ):
        raise ValueError("Image metadata exceeds its size limit")
    proof = json.loads((assets / "native-build.json").read_bytes())
    contract = release_contract(proof, proof["tag"], revision)
    manifest_hash = verify_component(assets / "ha_mcp_tools.zip", proof, contract)
    host = json.loads((assets / "published-verification.json").read_bytes())
    expected = {
        "repository": REPOSITORY,
        "source_revision": revision,
        "tag": proof["tag"],
        "wheel_url": contract["url"],
        "wheel_sha256": proof["files"][contract["wheel"]],
        "component_sha256": proof["files"]["ha_mcp_tools.zip"],
        "manifest_sha256": manifest_hash,
        "metadata_sha256": digest(assets / "native-build.json"),
    }
    if host != expected:
        raise ValueError("Image assets do not match the verified host receipt")
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(assets / "ha_mcp_tools.zip") as archive:
        archive.extractall(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetching = commands.add_parser("fetch")
    fetching.add_argument("--tag", required=True)
    fetching.add_argument("--checkout", type=Path, default=Path.cwd())
    fetching.add_argument("--output", type=Path, default=Path("dist/native"))
    image = commands.add_parser("prepare-image")
    image.add_argument("--assets", type=Path, required=True)
    image.add_argument("--destination", type=Path, required=True)
    image.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    if args.command == "fetch":
        print(json.dumps(fetch(args.checkout, args.output, args.tag), indent=2))
    else:
        prepare_image(args.assets, args.destination, args.source_revision)


if __name__ == "__main__":
    main()
