"""Core installs may upgrade compatible libraries without hiding unsafe changes."""

import runpy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.14.6",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_release": "fixture",
    "platform_system": "Linux",
    "platform_version": "fixture",
    "python_full_version": "3.14.6",
    "platform_python_implementation": "CPython",
    "python_version": "3.14",
    "sys_platform": "linux",
}


@pytest.fixture
def assess() -> Any:
    evaluate = runpy.run_path(
        str(ROOT / "tests/native/fixtures/native_fixture/dependency_policy.py")
    )["assess_dependency_changes"]

    def check(before: dict[str, str], after: dict[str, str], **kwargs: Any) -> dict:
        return evaluate(
            before,
            after,
            constraints=kwargs.pop("constraints", ""),
            marker_environment=kwargs.pop("marker_environment", ENVIRONMENT),
            **kwargs,
        )

    return check


def test_compatible_upgrades_are_recorded_for_every_package_not_an_allowlist(
    assess: Any,
) -> None:
    before = {
        "idna": "3.18",
        "dnspython": "2.8.0",
        "python-dotenv": "1.2.2",
        "future-library": "4.1",
    }
    after = {
        "idna": "3.20",
        "dnspython": "2.9.0",
        "python-dotenv": "1.2.4",
        "future-library": "4.9",
    }
    result = assess(before, after)
    assert result["verified"]
    assert set(result["compatible_upgrades"]) == set(after)
    assert result["changed_preinstalled"]["idna"] == {"before": "3.18", "after": "3.20"}


@pytest.mark.parametrize(
    "name", ["homeassistant", "mcp", "pydantic", "httpx", "cryptography"]
)
def test_preinstalled_sensitive_packages_cannot_change_even_compatibly(
    assess: Any, name: str
) -> None:
    result = assess({name: "2.1"}, {name: "2.2"})
    assert not result["verified"]
    assert result["rejected_changes"][0]["reason"] == "protected_package_changed"


@pytest.mark.parametrize(
    "before,after,reason",
    [
        ("2.2", "2.1", "downgrade"),
        ("2.2", None, "removed"),
        ("2.2", "3.0", "major_version_changed"),
        ("0.4.4", "0.5.0", "pre_one_minor_changed"),
        ("1.2", "1.3rc1", "stable_to_prerelease"),
        ("1.2", "1.3.dev1", "stable_to_prerelease"),
        ("1.2", "1!1.3", "epoch_changed"),
    ],
)
def test_unsafe_version_transitions_are_explained(
    assess: Any, before: str, after: str | None, reason: str
) -> None:
    result = assess(
        {"library": before}, {"library": after} if after is not None else {}
    )
    assert not result["verified"]
    assert result["rejected_changes"][0]["reason"] == reason


def test_pre_one_patch_and_prerelease_to_final_can_advance(assess: Any) -> None:
    assert assess({"library": "0.4.4"}, {"library": "0.4.6"})["verified"]
    assert assess({"library": "2.1rc1"}, {"library": "2.1"})["verified"]


def test_initial_additions_allow_the_candidate_dev_wheel_and_missing_sensitive_root(
    assess: Any,
) -> None:
    result = assess(
        {}, {"ha-mcp": "9.0.0.dev77", "httpx2": "2.13.1", "pydantic": "2.13.4"}
    )
    assert result["verified"] and set(result["added"]) == {
        "ha-mcp",
        "httpx2",
        "pydantic",
    }


def test_sensitive_root_added_at_setup_is_protected_on_the_next_observation(
    assess: Any,
) -> None:
    result = assess(
        {}, {"pydantic": "2.13.5"}, previous_versions={"pydantic": "2.13.4"}
    )
    assert not result["verified"]
    assert result["rejected_changes"][0]["reason"] == "protected_package_changed"


def test_an_unreadable_new_version_does_not_relabel_the_original_baseline_as_dirty(
    assess: Any,
) -> None:
    result = assess({}, {"library": "unknown"})
    assert result["baseline_clean"] and not result["verified"]


@pytest.mark.parametrize(
    "versions", [{"library": "unknown"}, {"library": "1", "Library": "2"}]
)
def test_unreadable_or_ambiguous_installed_versions_are_not_approved(
    assess: Any, versions: dict[str, str]
) -> None:
    result = assess(versions, versions)
    assert not result["verified"] and result["policy_errors"]


def test_new_conflicts_fail_while_unchanged_baseline_conflicts_remain_visible(
    assess: Any,
) -> None:
    assert assess({}, {}, before_issues=["known"], after_issues=["known"])["verified"]
    result = assess({}, {}, before_issues=["known"], after_issues=["known", "new"])
    assert not result["verified"] and result["new_issues"] == ["new"]


def test_core_constraints_use_the_core_interpreter_and_architecture_markers(
    assess: Any,
) -> None:
    constraints = "\n".join(
        [
            'library==1; python_version < "3.14"',
            'library==2; python_version >= "3.14" and platform_machine == "aarch64"',
            'library==3; python_version >= "3.14" and platform_machine == "x86_64"',
            "absent-optional==9",
        ]
    )
    assert assess({}, {"library": "2"}, constraints=constraints)["verified"]
    result = assess({}, {"library": "3"}, constraints=constraints)
    assert not result["verified"]
    assert result["constraint_violations"][0]["name"] == "library"


def test_unchanged_baseline_constraint_debt_is_visible_without_claiming_a_clean_image(
    assess: Any,
) -> None:
    result = assess({"library": "1"}, {"library": "1"}, constraints="library==2")
    assert result["verified"] and result["no_new_conflicts"]
    assert not result["baseline_clean"] and result["baseline_constraint_violations"]
    assert result["constraint_violations"] and not result["new_constraint_violations"]


def test_modifying_a_package_already_outside_core_constraints_does_not_grandfather_it(
    assess: Any,
) -> None:
    result = assess({"library": "1.0"}, {"library": "1.1"}, constraints="library==2")
    assert not result["verified"] and result["new_constraint_violations"]


@pytest.mark.parametrize(
    "constraint",
    [
        "-r other.txt",
        "-c other.txt",
        "https://example.test/library.whl",
        "library @ https://example.test/library.whl",
        "not a requirement",
        'library==1; "x" in dependency_groups',
    ],
)
def test_uninterpretable_core_constraints_fail_closed(
    assess: Any, constraint: str
) -> None:
    result = assess({}, {}, constraints=constraint)
    assert not result["verified"] and result["policy_errors"]


def test_missing_core_marker_environment_cannot_fall_back_to_the_runner(
    assess: Any,
) -> None:
    result = assess({}, {}, marker_environment={})
    assert not result["verified"] and result["policy_errors"]


@pytest.mark.parametrize("after", [{}, {"new": "1.0"}, {"new": "3.0"}])
def test_restart_cannot_hide_changes_to_packages_absent_from_original_baseline(
    assess: Any, after: dict[str, str]
) -> None:
    result = assess({}, after, previous_versions={"new": "2.0"})
    assert not result["verified"]
    assert result["rejected_changes"][0]["scope"] == "previous_observation"
