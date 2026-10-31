"""Native E2E must enforce the accepted policy through its actual guards."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from packaging.markers import default_environment

from tests.src.e2e import _conftest_native_runtime as harness
from tests.src.e2e.workflows.embedded import test_embedded_no_stomp as guard

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    collector = _load(
        "e2e_capture",
        ROOT / "tests/fixtures/native_qualification/dependency_capture.py",
    )
    receipt = _load(
        "e2e_receipt",
        ROOT / "tests/native/fixtures/native_fixture/dependency_receipt.py",
    )
    before = {
        "homeassistant": "2026.10.0",
        "websockets": "15.0.1",
        "certifi": "2026.7.22",
        "mcp": "1.16.0",
        "pydantic": "2.13.4",
        "httpx": "0.28.1",
        "cryptography": "47.0.0",
        "library": "2.1.0",
        "pre-one": "0.4.1",
    }
    state = SimpleNamespace(
        versions=dict(before), issues=["existing optional integration conflict"]
    )
    constraints = tmp_path / "package_constraints.txt"
    constraints.write_text("certifi>=2026.7.22\nlibrary<3\n", encoding="utf-8")
    environment = {
        **default_environment(),
        "sys_platform": "linux",
        "platform_machine": "aarch64",
    }

    def context(_path: Path | None = None) -> dict[str, Any]:
        raw = constraints.read_bytes()
        return {
            "text": raw.decode(),
            "path": str(constraints),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "marker_environment": environment,
        }

    monkeypatch.setattr(receipt, "installed_versions", lambda: dict(state.versions))
    monkeypatch.setattr(receipt, "dependency_issues", lambda: list(state.issues))
    monkeypatch.setattr(receipt, "core_constraint_context", context)
    monkeypatch.setattr(collector, "receipt_helpers", lambda: vars(receipt))
    component = tmp_path / "custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    manifest = component / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "requirements": [
                    "ha-mcp @ file:///config/candidate.whl",
                    "certifi>=2026.10.19",
                    "library>=2.1.0",
                    "missing-on-linux>=1; sys_platform == 'win32'",
                ]
            }
        ),
        encoding="utf-8",
    )

    def install(
        requirements: list[str], kwargs: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        assert state.versions == before
        assert kwargs == {"constraints": str(constraints)}
        state.versions["ha-mcp"] = "9.0.0.dev1"
        return requirements, []

    installer = Mock(side_effect=install)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.requirements",
        SimpleNamespace(
            _install_requirements_if_missing=installer,
            pip_kwargs=lambda _config: {"constraints": str(constraints)},
        ),
    )
    monkeypatch.setattr(guard, "native_runtime_selected", lambda: True)
    monkeypatch.delenv("E2E_ATTESTATION_DIRECTORY", raising=False)
    machine = SimpleNamespace(exec_run=Mock())
    info = {
        "config_path": str(tmp_path),
        "container": SimpleNamespace(get_wrapped_container=lambda: machine),
    }
    world = SimpleNamespace(
        config=tmp_path,
        collector=collector,
        receipt=receipt,
        before=before,
        state=state,
        installer=installer,
        constraints=constraints,
        manifest=manifest,
        info=info,
        machine=machine,
    )
    collector.prepare(tmp_path)
    state.versions["certifi"] = "2026.10.23"
    return world


def _preinstall_guard(world: Any) -> dict[str, Any]:
    proof = world.collector.observe(world.config, "after_core_setup")
    guard.test_embedded_preinstall_replaces_nothing_ha_governs(world.info)
    return proof


def _legacy_freezes(world: Any) -> None:
    for name, versions in (
        (guard.EMBEDDED_FREEZE_BEFORE, world.before),
        (guard.EMBEDDED_FREEZE_AFTER, world.state.versions),
    ):
        (world.config / name).write_text(
            "\n".join(f"{key}=={value}" for key, value in versions.items()),
            encoding="utf-8",
        )


def test_core_manifest_can_apply_the_required_certifi_security_upgrade(
    installation: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the old guard's real input, so reverting only the E2E dispatch
    # reproduces the observed certifi refusal instead of a missing-file error.
    _legacy_freezes(installation)
    monkeypatch.setattr(guard, "_governed_names", lambda _: {"certifi", "websockets"})
    proof = _preinstall_guard(installation)
    wheel, setup = proof["observations"]
    assert not wheel["changed_preinstalled"]
    assert setup["compatible_upgrades"]["certifi"] == {
        "before": "2026.7.22",
        "after": "2026.10.23",
    }
    assert setup["verified"] and not setup["baseline_clean"]
    assert proof["before_issues"] == setup["after_issues"]
    assert setup["previous_observation_assessment"]["verified"]
    installation.installer.assert_called_once()


def test_compatible_upgrade_policy_is_not_a_certifi_exception(
    installation: Any,
) -> None:
    installation.state.versions["library"] = "2.2.0"
    proof = _preinstall_guard(installation)
    assert "library" in proof["observations"][-1]["compatible_upgrades"]


@pytest.mark.parametrize(
    "name,version,reason",
    [
        ("library", "2.0.0", "downgrade"),
        ("library", None, "removed"),
        ("library", "3.0.0", "major_version_changed"),
        ("certifi", "2027.1.1", "major_version_changed"),
        ("pre-one", "0.5.0", "pre_one_minor_changed"),
        ("library", "2.2.0rc1", "stable_to_prerelease"),
        ("library", "1!2.2.0", "epoch_changed"),
        *[
            (name, version, "protected_package_changed")
            for name, version in (
                ("homeassistant", "2026.10.1"),
                ("mcp", "1.17.0"),
                ("pydantic", "2.13.5"),
                ("httpx", "0.28.2"),
                ("cryptography", "47.0.1"),
            )
        ],
    ],
)
def test_native_guard_rejects_unsafe_mutated_installations(
    installation: Any, name: str, version: str | None, reason: str
) -> None:
    if version is None:
        installation.state.versions.pop(name)
    else:
        installation.state.versions[name] = version
    with pytest.raises(AssertionError, match=reason):
        _preinstall_guard(installation)


def test_new_conflicts_are_refused_even_when_the_versions_are_compatible(
    installation: Any,
) -> None:
    installation.state.issues.append("new integration requires certifi<2026.10")
    with pytest.raises(AssertionError, match="new integration requires"):
        _preinstall_guard(installation)


@pytest.mark.parametrize("version", ["2026.7.22", None])
def test_native_manifest_floors_are_enforced_without_wheel_dependency_edges(
    installation: Any, version: str | None
) -> None:
    if version is None:
        installation.state.versions.pop("certifi")
    else:
        installation.state.versions["certifi"] = version
    with pytest.raises(AssertionError, match="manifest_requirement_unsatisfied"):
        _preinstall_guard(installation)


def test_core_constraints_are_enforced_for_new_distributions(installation: Any) -> None:
    path = installation.config / harness.NATIVE_DEPENDENCY_PROOF
    path.unlink()
    installation.state.versions = dict(installation.before)
    with installation.constraints.open("a", encoding="utf-8") as stream:
        stream.write("new-root<2; platform_machine == 'aarch64'\n")
    installation.collector.prepare(installation.config)
    installation.state.versions.update({"new-root": "2.0", "certifi": "2026.10.23"})
    with pytest.raises(AssertionError, match="version_outside_core_constraint"):
        _preinstall_guard(installation)


def test_constraint_changes_do_not_redefine_the_original_contract(
    installation: Any,
) -> None:
    installation.constraints.write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match="core_constraint_context_changed"):
        _preinstall_guard(installation)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"certifi": "2026.10.23"}, "paired_wheel_changed_preinstalled"),
        ({"extra-tree": "1.0"}, "paired_wheel_unexpected_additions"),
    ],
)
def test_initial_wheel_cannot_move_or_add_a_dependency_even_compatibly(
    installation: Any, mutation: dict[str, str], reason: str
) -> None:
    path = installation.config / harness.NATIVE_DEPENDENCY_PROOF
    path.unlink()
    installation.state.versions = dict(installation.before)
    original = installation.installer.side_effect

    def install(*args: Any) -> Any:
        result = original(*args)
        installation.state.versions.update(mutation)
        return result

    installation.installer.side_effect = install
    with pytest.raises(RuntimeError, match="Paired wheel violated"):
        installation.collector.prepare(installation.config)
    proof = json.loads(path.read_text(encoding="utf-8"))
    assert reason in {
        row["reason"] for row in proof["observations"][-1]["policy_errors"]
    }
    with pytest.raises(AssertionError, match="earlier installation failed"):
        installation.collector.prepare(installation.config)
    assert installation.installer.call_count == 2


@pytest.mark.parametrize("mutate", ["new_package_downgrade", "reintroduced_conflict"])
def test_consecutive_observations_cannot_hide_a_regression_against_setup(
    installation: Any, mutate: str
) -> None:
    installation.state.versions["new-root"] = "1.2"
    installation.state.issues.clear()
    _preinstall_guard(installation)
    if mutate == "new_package_downgrade":
        installation.state.versions["new-root"] = "1.1"
    else:
        installation.state.issues.append("existing optional integration conflict")
    proof = installation.collector.observe(installation.config, "runtime")
    installation.machine.exec_run.return_value = SimpleNamespace(
        exit_code=1, output=json.dumps(proof).encode()
    )
    with pytest.raises(AssertionError, match="previous_observation"):
        guard.test_embedded_runtime_install_replaces_nothing_ha_governs(
            installation.info
        )


def test_runtime_guard_captures_then_checks_provenance_and_preserves_evidence(
    installation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _preinstall_guard(installation)
    evidence = installation.config / "artifacts"
    monkeypatch.setenv("E2E_ATTESTATION_DIRECTORY", str(evidence))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    provenance = Mock()
    monkeypatch.setattr(guard, "_assert_native_managed_install", provenance)

    def execute(command: list[str]) -> Any:
        assert command == harness.native_dependency_command("runtime")
        proof = installation.collector.observe(installation.config, "runtime")
        return SimpleNamespace(exit_code=0, output=json.dumps(proof).encode())

    installation.machine.exec_run.side_effect = execute
    guard.test_embedded_runtime_install_replaces_nothing_ha_governs(installation.info)
    provenance.assert_called_once_with(
        installation.info, installation.before, installation.state.versions
    )
    proof = json.loads((evidence / "native-dependencies-gw1.json").read_bytes())
    assert [row["phase"] for row in proof["observations"]] == [
        "paired_wheel",
        "after_core_setup",
        "runtime",
    ]


def test_legacy_profile_keeps_its_strict_governed_package_guard(
    installation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "native_runtime_selected", lambda: False)
    monkeypatch.setattr(guard, "_governed_names", lambda _: {"certifi"})
    _legacy_freezes(installation)
    with pytest.raises(AssertionError, match="HA GOVERNS"):
        guard.test_embedded_preinstall_replaces_nothing_ha_governs(installation.info)


def test_a_later_clean_sample_cannot_erase_a_rejected_observation(
    installation: Any,
) -> None:
    installation.state.issues.append("new conflict")
    with pytest.raises(AssertionError, match="new conflict"):
        _preinstall_guard(installation)
    installation.state.issues.pop()
    with pytest.raises(AssertionError, match="new conflict"):
        _preinstall_guard(installation)
    proof = json.loads(
        (installation.config / harness.NATIVE_DEPENDENCY_PROOF).read_bytes()
    )
    assert not proof["observations"][-1]["verified"]
    assert {"reason": "previous_observation_rejected"} in proof["observations"][-1][
        "policy_errors"
    ]


def test_reentering_boot_keeps_the_baseline_and_never_reinstalls(
    installation: Any,
) -> None:
    original = _preinstall_guard(installation)
    proof = installation.collector.prepare(installation.config)
    assert proof["before"] == original["before"]
    assert proof["before_issues"] == original["before_issues"]
    assert proof["observations"][-1]["phase"] == "before_core_restart"
    installation.installer.assert_called_once()


@pytest.mark.parametrize("missing", ["baseline", "paired_wheel", "after_core_setup"])
def test_incomplete_phase_evidence_cannot_qualify(
    installation: Any, missing: str
) -> None:
    proof = _preinstall_guard(installation)
    if missing == "baseline":
        del proof["before_issues"]
    else:
        proof["observations"] = [
            row for row in proof["observations"] if row["phase"] != missing
        ]
    (installation.config / harness.NATIVE_DEPENDENCY_PROOF).write_text(
        json.dumps(proof), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="Missing"):
        guard.test_embedded_preinstall_replaces_nothing_ha_governs(installation.info)


def test_a_broken_conflict_capture_is_not_an_empty_success(
    installation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        installation.receipt,
        "dependency_issues",
        Mock(side_effect=RuntimeError("pip check unavailable")),
    )
    with pytest.raises(RuntimeError, match="pip check unavailable"):
        _preinstall_guard(installation)


def test_manifest_rewriting_during_setup_cannot_relax_its_floors(
    installation: Any,
) -> None:
    manifest = json.loads(installation.manifest.read_bytes())
    manifest["requirements"].remove("certifi>=2026.10.19")
    installation.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match="manifest_changed"):
        _preinstall_guard(installation)


def test_source_harness_resolves_the_unmodified_shared_receipt_and_policy(
    tmp_path: Path,
) -> None:
    harness.stage_native_dependency_capture(ROOT, tmp_path)
    module = _load(
        "staged_capture", tmp_path / "native-dependency-harness/dependency_capture.py"
    )
    helpers = module.receipt_helpers()
    policy = helpers["_dependency_policy"]()
    assert policy.__name__ == "assess_dependency_changes"
    for name in ("dependency_receipt.py", "dependency_policy.py"):
        assert (tmp_path / "native-dependency-harness" / name).read_bytes() == (
            ROOT / "tests/native/fixtures/native_fixture" / name
        ).read_bytes()
    assert not (tmp_path / "custom_components/ha_mcp_tools").exists()
