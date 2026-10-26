"""Dependency evidence in disposable Core, separate from the candidate package.

The entrypoint brackets only the dependency-free wheel with Core's installer.
Core itself processes manifest roots during boot. Later samples inspect that
environment; they never reconstruct a baseline, retry installs or roll back.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
from functools import cache
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROOF_NAME = "native-dependencies.json"


@cache
def receipt_helpers() -> dict[str, Any]:
    return runpy.run_path(str(Path(__file__).with_name("dependency_receipt.py")))


def _save(path: Path, proof: dict[str, Any]) -> None:
    path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")


def _manifest(config: Path) -> tuple[dict[str, Any], str]:
    raw = (config / "custom_components/ha_mcp_tools/manifest.json").read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _requirement_errors(
    requirements: list[str], versions: dict[str, str], environment: dict[str, str]
) -> list[dict[str, Any]]:
    errors = []
    for raw in requirements:
        requirement = Requirement(raw)
        if requirement.marker and not requirement.marker.evaluate(
            environment={**environment, "extra": ""}
        ):
            continue
        name = canonicalize_name(requirement.name)
        version = versions.get(name)
        if version is None or (
            not requirement.url and not requirement.specifier.contains(version)
        ):
            errors.append(
                {
                    "reason": "manifest_requirement_unsatisfied",
                    "requirement": raw,
                    "name": name,
                    "version": version,
                }
            )
    return errors


def observe(config: Path, phase: str) -> dict[str, Any]:
    path = config / PROOF_NAME
    helpers = receipt_helpers()
    proof = helpers["observe_dependencies"](
        config / "custom_components/ha_mcp_tools", path=path, phase=phase
    )
    observation = proof["observations"][-1]
    errors = observation["policy_errors"]
    manifest, digest = _manifest(config)
    if digest != proof["manifest_sha256"]:
        errors.append({"reason": "manifest_changed"})
    if proof.get("installation_failure"):
        errors.append({"reason": "previous_installation_failed"})
    if len(proof["observations"]) > 1:
        previous = proof["observations"][-2]
        transition = helpers["assess_dependencies"](
            previous["after"],
            observation["after"],
            previous["after_issues"],
            observation["after_issues"],
            baseline_constraints=proof["core_constraints_baseline"],
        )
        observation["previous_observation_assessment"] = transition
        if not transition["verified"]:
            errors.append({"reason": "previous_observation_transition_rejected"})
    if phase == "paired_wheel":
        if observation["changed_preinstalled"]:
            errors.append({"reason": "paired_wheel_changed_preinstalled"})
        if set(observation["added"]) != {"ha-mcp"}:
            errors.append({"reason": "paired_wheel_unexpected_additions"})
    else:
        errors.extend(
            _requirement_errors(
                manifest["requirements"],
                observation["after"],
                observation["core_constraints"]["marker_environment"],
            )
        )
    observation["manifest_sha256"] = digest
    observation["verified"] = observation["policy_passed"] = (
        observation["verified"] and not errors
    )
    _save(path, proof)
    return proof


def assert_qualified(proof: dict[str, Any], *, runtime: bool = False) -> None:
    """Do not turn absent phase evidence or a previously rejected phase green."""
    assert proof.get("mode") == "candidate", "Missing native candidate baseline"
    assert not proof.get("installation_failure"), "An earlier installation failed"
    assert proof.get("before", {}).get("homeassistant"), "Missing Core baseline"
    assert not {"ha-mcp", "ha-mcp-dev"} & proof["before"].keys(), "Not a cold baseline"
    assert "before_issues" in proof and proof.get("core_constraints_baseline"), (
        "Missing pre-install conflicts or Core constraints"
    )
    observations = proof.get("observations", [])
    phases = [item["phase"] for item in observations]
    assert phases and phases[0] == "paired_wheel", "Missing isolated wheel observation"
    assert "after_core_setup" in phases, "Missing completed Core manifest observation"
    if runtime:
        assert phases[-1] == "runtime", "Missing live runtime observation"
    for observation in observations:
        assert observation.get("verified") is True, json.dumps(
            observation, sort_keys=True
        )


def prepare(config: Path) -> dict[str, Any]:
    """Install the paired wheel once, using unmodified Core kwargs and decisions."""
    from homeassistant.requirements import _install_requirements_if_missing, pip_kwargs

    path = config / PROOF_NAME
    if path.exists():
        # Docker restarts can rerun the entrypoint; never overwrite the baseline
        # or recover an uncertain prior installation by trying it again.
        proof = json.loads(path.read_text(encoding="utf-8"))
        assert_qualified(proof)
        return observe(config, "before_core_restart")
    helpers = receipt_helpers()
    helpers["capture_baseline"](path)
    proof = json.loads(path.read_text(encoding="utf-8"))
    manifest, digest = _manifest(config)
    paired = [
        raw
        for raw in manifest["requirements"]
        if canonicalize_name(Requirement(raw).name) == "ha-mcp"
    ]
    if len(paired) != 1:
        raise ValueError("Native manifest must have exactly one paired wheel")
    context = helpers["core_constraint_context"]()
    proof.update(
        mode="candidate",
        manifest_sha256=digest,
        manifest_requirements=manifest["requirements"],
        core_constraints_text=context["text"],
    )
    _save(path, proof)
    try:
        kwargs = pip_kwargs(str(config))
        if str(kwargs["constraints"]) != proof["core_constraints_baseline"]["path"]:
            raise ValueError("Core installation and observation constraints differ")
        installed, failed = _install_requirements_if_missing(paired, kwargs)
        proof = observe(config, "paired_wheel")
        if failed or paired[0] not in installed:
            raise RuntimeError("Core refused the paired wheel")
        if not proof["observations"][-1]["verified"]:
            raise RuntimeError("Paired wheel violated the dependency policy")
    except BaseException as error:
        proof = json.loads(path.read_text(encoding="utf-8"))
        proof["installation_failure"] = {"type": type(error).__name__}
        _save(path, proof)
        raise
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "after_core_setup", "runtime"))
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    proof = (
        prepare(args.config)
        if args.phase == "prepare"
        else observe(args.config, args.phase)
    )
    print(json.dumps(proof))
    if not proof["observations"][-1]["verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
