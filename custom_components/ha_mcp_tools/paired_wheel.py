"""Read-only PEP 610 evidence for a component-paired wheel already installed."""

import importlib.metadata
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version


def paired_wheel_matches(
    spec: str, installed_version: str | None, target_version: str | None
) -> bool:
    """Prove URL, wheel version and SHA-256; absence or ambiguity is a miss.

    This is installation metadata, not a signature or a scan of installed files.
    The caller must separately exclude overrides, reinstall requests and a
    conflicting distribution before allowing the ordinary no-install path.
    """
    if installed_version is None or target_version is None:
        return False
    try:
        requirement = Requirement(spec)
        name = canonicalize_name(requirement.name)
        if (
            name not in {"ha-mcp", "ha-mcp-dev"}
            or not requirement.url
            or requirement.marker
            or requirement.extras
        ):
            return False
        url = urlsplit(requirement.url)
        if (
            url.scheme != "https"
            or not url.netloc
            or url.username
            or url.password
            or url.query
        ):
            return False
        if re.fullmatch(r"sha256=[a-fA-F0-9]{64}", url.fragment) is None:
            return False
        wheel_name, wheel_version, _build, _tags = parse_wheel_filename(
            unquote(url.path.rsplit("/", 1)[-1])
        )
        if wheel_name != name or not (
            wheel_version == Version(installed_version) == Version(target_version)
        ):
            return False
        distribution = importlib.metadata.distribution(name)
        if (
            Version(distribution.version) != wheel_version
            or canonicalize_name(distribution.metadata["Name"]) != name
        ):
            return False
        receipt = json.loads(distribution.read_text("direct_url.json") or "null")
        return _receipt_matches(
            receipt, urlunsplit(url._replace(fragment="")), url.fragment[7:].lower()
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        importlib.metadata.PackageNotFoundError,
    ):
        return False


def _receipt_matches(receipt: object, url: str, digest: str) -> bool:
    if not isinstance(receipt, dict) or receipt.get("url") != url:
        return False
    if any(key in receipt for key in ("dir_info", "vcs_info", "subdirectory")):
        return False
    info = receipt.get("archive_info")
    if not isinstance(info, dict):
        return False
    hashes = info.get("hashes")
    legacy_hash = info.get("hash")
    if hashes is not None:
        return (
            isinstance(hashes, dict)
            and hashes.get("sha256") == digest
            and (legacy_hash is None or legacy_hash == "sha256=" + digest)
        )
    return legacy_hash == "sha256=" + digest
