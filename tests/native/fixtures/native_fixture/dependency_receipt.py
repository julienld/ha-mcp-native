"""Test-only evidence of Core's managed installation of the public wheel.

Both collection functions run outside the HA event loop. No install command is
issued here: the integration's unmodified requirements belong to Core.
"""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import subprocess
import sys
from functools import cache
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.markers import default_environment

PROOF = Path("/opt/native-dependencies.json")
ASSETS = Path("/opt/native-assets")


@cache
def _dependency_policy() -> Any:
    # This collector also runs standalone during image construction and via
    # runpy in the minimal worker, without importing the HA fixture package.
    return runpy.run_path(str(Path(__file__).with_name("dependency_policy.py")))[
        "assess_dependency_changes"
    ]


def core_constraint_context(constraint_path: Path | None = None) -> dict[str, Any]:
    if constraint_path is None:
        from homeassistant.requirements import pip_kwargs

        constraint_path = Path(pip_kwargs(None)["constraints"])
    raw = constraint_path.read_bytes()
    return {
        "text": raw.decode("utf-8"),
        "path": str(constraint_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "marker_environment": default_environment(),
    }


def assess_dependencies(
    before_versions: dict[str, str],
    after_versions: dict[str, str],
    before_issues: list[str],
    after_issues: list[str],
    *,
    constraint_path: Path | None = None,
    previous_versions: dict[str, str] | None = None,
    baseline_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context_error = None
    try:
        context = core_constraint_context(constraint_path)
    except (OSError, UnicodeError, ImportError, KeyError, TypeError) as error:
        context_error = {
            "reason": "core_constraints_unreadable",
            "error_type": type(error).__name__,
        }
        context = {"text": "", "marker_environment": default_environment()}
    result = _dependency_policy()(
        before_versions,
        after_versions,
        before_issues=before_issues,
        after_issues=after_issues,
        constraints=context["text"],
        marker_environment=context["marker_environment"],
        previous_versions=previous_versions,
    )
    result["core_constraints"] = {
        key: value for key, value in context.items() if key != "text"
    }
    if context_error is not None:
        result["policy_errors"].append(context_error)
    context_changed = False
    if baseline_constraints is not None:
        for key in ("sha256", "marker_environment"):
            if key not in baseline_constraints or baseline_constraints[
                key
            ] != context.get(key):
                context_changed = True
                result["policy_errors"].append(
                    {"reason": "core_constraint_context_changed", "field": key}
                )
    if context_error is not None or context_changed:
        result.update(
            verified=False,
            policy_passed=False,
            no_new_conflicts=False,
            baseline_clean=False,
        )
    return result


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
    if "homeassistant" not in versions:
        raise ValueError("Core baseline inventory has no Home Assistant distribution")
    context = core_constraint_context()
    path.write_text(
        json.dumps(
            {
                "mode": "published",
                "before": versions,
                "before_issues": dependency_issues(),
                "core_constraints_baseline": {
                    key: value for key, value in context.items() if key != "text"
                },
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
    expected: dict[str, Any],
    *,
    evidence: dict[str, Any] | None = None,
    index_path: Path | None = None,
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
        if (
            archive not in ({}, {"hashes": {}})
            or index_path is None
            or not index_path.is_file()
        ):
            raise ValueError("Installed wheel receipt has no SHA-256 digest")
        evidence["payload"] = _verify_payload(distribution, index_path, expected)
        return {
            "name": "ha-mcp",
            "version": distribution.version,
            "url": expected["wheel_url"],
            "sha256": None,
            "payload": evidence["payload"],
        }
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


def _verify_payload(
    distribution: Any, index: Path, expected: dict[str, Any]
) -> dict[str, Any]:
    from custom_components.ha_mcp_tools.wheel_payload import verify_wheel_payload

    return verify_wheel_payload(
        distribution,
        "ha-mcp",
        distribution.version,
        expected["wheel_sha256"],
        index_path=index,
    )


def _public_observation(
    component: Path, assets: Path, proof: dict[str, Any], observation: dict[str, Any]
) -> None:
    expected = json.loads((assets / "published-verification.json").read_bytes())
    release = json.loads((assets / "native-build.json").read_bytes())
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
        observation["wheel"] = wheel_receipt(
            expected,
            evidence=observation["receipt"],
            index_path=component / "native-wheel.json",
        )
        if observation["wheel"]["version"] != release["library_version"]:
            observation["errors"].append(
                "Installed wheel version differs from the release"
            )
    except (OSError, ValueError, metadata.PackageNotFoundError) as error:
        observation["errors"].append(str(error))
    proof["release"] = expected


def observe_dependencies(
    component: Path,
    path: Path = PROOF,
    assets: Path = ASSETS,
    phase: str = "unspecified",
) -> dict[str, Any]:
    proof: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if proof.get("mode") not in {"candidate", "published"}:
        raise ValueError("Unknown installation qualification mode")
    after = installed_versions()
    issues = dependency_issues()
    preceding = proof.get("observations", [])
    observation: dict[str, Any] = {
        "phase": phase,
        "after": after,
        "after_issues": issues,
        **assess_dependencies(
            proof["before"],
            after,
            proof["before_issues"],
            issues,
            previous_versions=preceding[-1]["after"] if preceding else None,
            baseline_constraints=proof.get("core_constraints_baseline", {}),
        ),
        "errors": [],
    }
    if any(item.get("verified") is not True for item in preceding):
        observation["policy_errors"].append({"reason": "previous_observation_rejected"})
        observation["verified"] = False
        observation["policy_passed"] = False
    if proof["mode"] == "published":
        _public_observation(component, assets, proof, observation)
    observation["verified"] = observation["verified"] and not observation["errors"]
    proof.setdefault("observations", []).append(observation)
    # Keep the existing probe's dependency fields useful after reload/restart.
    proof.update({key: observation[key] for key in ("after", "after_issues")})
    path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return proof
