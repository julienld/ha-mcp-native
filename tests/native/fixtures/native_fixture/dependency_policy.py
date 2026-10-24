"""Pure qualification policy for Core-managed dependency changes."""

from collections.abc import Mapping, Sequence
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

POLICY_VERSION = "compatible-upgrades-v1"
PROTECTED_PACKAGES = frozenset(
    {"homeassistant", "mcp", "pydantic", "httpx", "cryptography"}
)
MARKER_KEYS = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "platform_python_implementation",
        "python_version",
        "sys_platform",
    }
)


def _versions(
    values: Mapping[str, str], scope: str, errors: list[dict[str, Any]]
) -> tuple[dict[str, str], dict[str, Version]]:
    normalized: dict[str, str] = {}
    parsed: dict[str, Version] = {}
    for raw_name, raw_version in values.items():
        name = canonicalize_name(raw_name)
        if name in normalized:
            errors.append(
                {"reason": "ambiguous_distribution", "name": name, "scope": scope}
            )
            continue
        normalized[name] = raw_version
        try:
            parsed[name] = Version(raw_version)
        except (InvalidVersion, TypeError):
            errors.append(
                {
                    "reason": "invalid_version",
                    "name": name,
                    "version": raw_version,
                    "scope": scope,
                }
            )
    return normalized, parsed


def _refusal(name: str, before: Version, after: Version) -> str | None:
    if name in PROTECTED_PACKAGES:
        return "protected_package_changed"
    if before.epoch != after.epoch:
        return "epoch_changed"
    if after < before:
        return "downgrade"
    if not before.is_prerelease and after.is_prerelease:
        return "stable_to_prerelease"
    if before.major != after.major:
        return "major_version_changed"
    if before.major == 0 and before.minor != after.minor:
        return "pre_one_minor_changed"
    return None


def _transitions(
    before: Mapping[str, str],
    after: Mapping[str, str],
    parsed_before: Mapping[str, Version],
    parsed_after: Mapping[str, Version],
    scope: str,
    rejected: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    changed: dict[str, Any] = {}
    compatible: dict[str, Any] = {}
    for name, old in before.items():
        if after.get(name) == old:
            continue
        delta = {"before": old, "after": after.get(name)}
        changed[name] = delta
        if name not in after:
            reason = "removed"
        elif name not in parsed_before or name not in parsed_after:
            reason = "invalid_version"
        else:
            reason = _refusal(name, parsed_before[name], parsed_after[name])
        if reason:
            rejected.append({"name": name, **delta, "reason": reason, "scope": scope})
        else:
            compatible[name] = delta
    return changed, compatible


def _active_constraints(
    constraints: str,
    environment: Mapping[str, str],
    errors: list[dict[str, Any]],
) -> list[Requirement]:
    missing = sorted(MARKER_KEYS - environment.keys())
    if missing:
        errors.append({"reason": "missing_core_marker_environment", "keys": missing})
        return []
    active: list[Requirement] = []
    for line_number, raw in enumerate(constraints.splitlines(), start=1):
        line = raw.split(" #", maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            errors.append({"reason": "invalid_core_constraint", "line": line_number})
            continue
        try:
            applies = requirement.marker is None or requirement.marker.evaluate(
                environment={**environment, "extra": ""}
            )
        except (KeyError, ValueError, TypeError):
            errors.append({"reason": "unsupported_core_marker", "line": line_number})
            continue
        if not applies:
            continue
        if requirement.url or requirement.extras:
            errors.append(
                {"reason": "unsupported_core_constraint", "line": line_number}
            )
            continue
        active.append(requirement)
    return active


def _constraint_violations(
    requirements: Sequence[Requirement],
    after: Mapping[str, str],
    parsed_after: Mapping[str, Version],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        if name not in after or name not in parsed_after:
            continue
        if not requirement.specifier.contains(parsed_after[name], prereleases=True):
            violations.append(
                {
                    "name": name,
                    "version": after[name],
                    "requirement": str(requirement),
                    "reason": "version_outside_core_constraint",
                }
            )
    return violations


def assess_dependency_changes(
    before_versions: Mapping[str, str],
    after_versions: Mapping[str, str],
    *,
    constraints: str,
    marker_environment: Mapping[str, str],
    before_issues: Sequence[str] = (),
    after_issues: Sequence[str] = (),
    previous_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Accept compatible advances, retaining all changes and refusal evidence.

    The optional preceding observation catches removals/downgrades of packages
    first added during setup; they did not exist in the original Core baseline.
    Constraints limit installed packages, not the set of packages that must exist.
    """
    errors: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    before, parsed_before = _versions(before_versions, "baseline", errors)
    baseline_version_errors = bool(errors)
    after, parsed_after = _versions(after_versions, "current", errors)
    changed, compatible = _transitions(
        before, after, parsed_before, parsed_after, "baseline", rejected
    )
    transition_changes: dict[str, Any] = {}
    transition_upgrades: dict[str, Any] = {}
    if previous_versions is not None:
        previous, parsed_previous = _versions(
            previous_versions, "previous_observation", errors
        )
        transition_changes, transition_upgrades = _transitions(
            previous,
            after,
            parsed_previous,
            parsed_after,
            "previous_observation",
            rejected,
        )
    previous_error_count = len(errors)
    active = _active_constraints(constraints, marker_environment, errors)
    constraint_inspection_failed = len(errors) != previous_error_count
    violations = _constraint_violations(active, after, parsed_after)
    baseline_violations = _constraint_violations(active, before, parsed_before)
    new_violations = [
        violation
        for violation in violations
        if violation not in baseline_violations
        or before.get(violation["name"]) != after[violation["name"]]
        or violation["name"] in transition_changes
    ]
    new_issues = sorted(set(after_issues) - set(before_issues))
    passed = not (rejected or new_issues or new_violations or errors)
    return {
        "policy_version": POLICY_VERSION,
        "verification_scope": "dependency_policy_only",
        "changed_preinstalled": changed,
        "compatible_upgrades": compatible,
        "added": {
            name: version for name, version in after.items() if name not in before
        },
        "transition_changes": transition_changes,
        "transition_upgrades": transition_upgrades,
        "rejected_changes": rejected,
        "new_issues": new_issues,
        "constraint_violations": violations,
        "baseline_constraint_violations": baseline_violations,
        "new_constraint_violations": new_violations,
        "baseline_clean": not (
            before_issues
            or baseline_violations
            or baseline_version_errors
            or constraint_inspection_failed
        ),
        "no_new_conflicts": not (new_issues or new_violations or errors),
        "policy_errors": errors,
        "policy_passed": passed,
        "verified": passed,
    }
