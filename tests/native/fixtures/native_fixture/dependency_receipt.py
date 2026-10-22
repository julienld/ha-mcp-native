"""Test-only evidence of Core's managed installation of the public wheel.

Both collection functions run outside the HA event loop. No install command is
issued here: the integration's unmodified requirements belong to Core.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

PROOF = Path("/opt/native-dependencies.json")
ASSETS = Path("/opt/native-assets")


def installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        if name in versions:
            raise ValueError(f"Ambiguous installed distribution: {name}")
        versions[name] = distribution.version
    return dict(sorted(versions.items()))


def dependency_issues() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode == 0:
        return []
    if result.returncode != 1 or result.stderr.strip():
        raise RuntimeError("pip check could not inspect the environment")
    issues = sorted(set(result.stdout.splitlines()))
    if not issues:
        raise RuntimeError("pip check failed without dependency diagnostics")
    return issues


def capture_baseline(path: Path = PROOF) -> None:
    versions = installed_versions()
    if {"ha-mcp", "ha-mcp-dev"} & versions.keys():
        raise ValueError("Published lane must start without either HA-MCP distribution")
    path.write_text(
        json.dumps(
            {
                "mode": "published",
                "before": versions,
                "before_issues": dependency_issues(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _safe_archive_info(direct: dict[str, Any]) -> dict[str, Any] | str | None:
    """Retain hash evidence, never URL queries or arbitrary installer fields."""
    archive = direct.get("archive_info")
    if archive is None:
        return None
    if not isinstance(archive, dict):
        return "<invalid>"
    safe: dict[str, Any] = {}
    if "hashes" in archive:
        hashes = archive["hashes"]
        if not isinstance(hashes, dict):
            safe["hashes"] = "<invalid>"
        else:
            safe["hashes"] = {}
            if "sha256" in hashes:
                digest = hashes["sha256"]
                safe["hashes"]["sha256"] = (
                    digest
                    if isinstance(digest, str)
                    and re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                    else "<invalid>"
                )
    if "hash" in archive:
        legacy = archive["hash"]
        safe["hash"] = (
            legacy
            if isinstance(legacy, str)
            and re.fullmatch(r"[a-z0-9_-]{1,20}=[a-fA-F0-9]{16,128}", legacy)
            else "<invalid>"
        )
    return safe


def wheel_receipt(
    expected: dict[str, Any], *, evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    if evidence is None:
        evidence = {}
    distribution = metadata.distribution("ha-mcp")
    if distribution.metadata["Name"] != "ha-mcp":
        raise ValueError("Installed distribution name differs from the released wheel")
    direct = json.loads(distribution.read_text("direct_url.json") or "null")
    if not isinstance(direct, dict):
        raise ValueError("Installed wheel has no PEP 610 receipt")
    evidence.update(
        {
            "url_matches": direct.get("url") == expected["wheel_url"],
            "archive_info": _safe_archive_info(direct),
            "sha256_status": "unverified",
        }
    )
    if direct.get("url") != expected["wheel_url"] or any(
        key in direct for key in ("dir_info", "vcs_info", "subdirectory")
    ):
        raise ValueError("Installed wheel did not originate at the public manifest URL")
    archive = direct.get("archive_info", {})
    if not isinstance(archive, dict) or not isinstance(archive.get("hashes", {}), dict):
        raise ValueError("Installed wheel has malformed archive hash evidence")
    actual = archive.get("hashes", {}).get("sha256")
    old = archive.get("hash")
    if actual is None and isinstance(old, str) and old.startswith("sha256="):
        actual = old.removeprefix("sha256=")
    if actual is None:
        evidence["sha256_status"] = "missing"
        raise ValueError("Installed wheel receipt has no SHA-256 digest")
    if actual != expected["wheel_sha256"] or (
        old is not None and old != "sha256=" + expected["wheel_sha256"]
    ):
        evidence["sha256_status"] = "mismatch"
        raise ValueError("Installed wheel archive digest differs from the release")
    evidence["sha256_status"] = "match"
    # Only the verified public URL is returned, never arbitrary receipt fields.
    return {
        "name": "ha-mcp",
        "version": distribution.version,
        "url": expected["wheel_url"],
        "sha256": actual,
    }


def observe_dependencies(
    component: Path,
    path: Path = PROOF,
    assets: Path = ASSETS,
) -> dict[str, Any]:
    proof: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if proof.get("mode") != "published":
        raise ValueError("Managed-install receipt is specific to the published lane")
    expected = json.loads((assets / "published-verification.json").read_bytes())
    release = json.loads((assets / "native-build.json").read_bytes())
    after = installed_versions()
    issues = dependency_issues()
    observation: dict[str, Any] = {
        "after": after,
        "after_issues": issues,
        "changed_preinstalled": {
            name: {"before": version, "after": after.get(name)}
            for name, version in proof["before"].items()
            if after.get(name) != version
        },
        "new_issues": sorted(set(issues) - set(proof["before_issues"])),
        "errors": [],
    }
    raw = (component / "manifest.json").read_bytes()
    observation["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    if observation["manifest_sha256"] != expected["manifest_sha256"]:
        observation["errors"].append(
            "The published manifest changed before or during setup"
        )
    if release["dependency"] not in manifest.get("requirements", []):
        observation["errors"].append("The published wheel requirement was rewritten")
    try:
        observation["receipt"] = {}
        observation["wheel"] = wheel_receipt(expected, evidence=observation["receipt"])
        if observation["wheel"]["version"] != release["library_version"]:
            observation["errors"].append(
                "Installed wheel version differs from the release"
            )
    except (ValueError, metadata.PackageNotFoundError) as error:
        observation["errors"].append(str(error))
    observation["verified"] = not (
        observation["changed_preinstalled"]
        or observation["new_issues"]
        or observation["errors"]
    )
    proof["release"] = expected
    proof.setdefault("observations", []).append(observation)
    # Keep the existing probe's dependency fields useful after reload/restart.
    proof.update({key: observation[key] for key in ("after", "after_issues")})
    path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return proof
