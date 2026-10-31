"""The minimal lane must expose changes hidden by a preinstalled Core image."""

import copy
import importlib.metadata
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from tests.native import minimal_install, minimal_probes, minimal_qualification
from tests.native.minimal_support import changes, sanitize


@pytest.fixture
def core_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.13,<3.15"\n'
        '[tool.uv]\nexclude-newer = "7 days"\n'
        'exclude-newer-package = {certifi = "2026-10-21T00:00:00Z"}\n'
    )
    monkeypatch.setattr(minimal_qualification, "ROOT", checkout)
    package = tmp_path / "image/homeassistant"
    package.mkdir(parents=True)
    origin = package / "__init__.py"
    origin.write_text("")
    constraints = package / "package_constraints.txt"
    constraints.write_text("# Bootstrap constraints shipped by this image\n")
    frontend = package / "components/frontend/manifest.json"
    frontend.parent.mkdir(parents=True)
    frontend.write_text(
        json.dumps({"requirements": ["home-assistant-frontend==20260930.5"]})
    )
    world = SimpleNamespace(core="2026.8.0", uv="0.11.31", constraints=constraints)
    world.distribution = SimpleNamespace(
        version=world.core,
        files=[Path("homeassistant/package_constraints.txt")],
        locate_file=lambda filename: package.parent / filename,
        read_text=lambda _filename: None,
    )
    monkeypatch.setattr(
        minimal_qualification,
        "sys",
        SimpleNamespace(
            platform="linux",
            version_info=(3, 14, 2),
            executable=sys.executable,
            version=sys.version,
            implementation=sys.implementation,
        ),
    )
    monkeypatch.setattr(
        minimal_qualification.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin=str(origin)),
    )
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: (
            world.distribution
            if name == "homeassistant"
            else SimpleNamespace(version=world.uv)
        ),
    )
    monkeypatch.setattr(minimal_qualification.venv, "EnvBuilder", Mock())
    monkeypatch.setattr(
        minimal_qualification, "attest_runtime", Mock(return_value={"verified": True})
    )
    monkeypatch.setattr(
        minimal_qualification,
        "datetime",
        SimpleNamespace(now=lambda _: datetime(2026, 10, 26, tzinfo=UTC)),
    )
    world.run = Mock()
    monkeypatch.setattr(minimal_qualification, "run_logged", world.run)
    world.output = tmp_path / "proof.json"
    world.qualify = lambda: minimal_qualification.qualify(
        tmp_path / "assets",
        tmp_path / "work",
        world.output,
        image_receipt=tmp_path / "image.json",
        expected_core=world.core,
        expected_arch="amd64",
        python_source="3.14.5",
    )
    return world


@pytest.mark.parametrize(
    ("core_version", "uv_version"),
    [("2026.8.0", "0.11.31"), ("2026.10.3", "0.13.0")],
)
def test_minimal_lane_installs_the_image_core_and_its_own_uv(
    core_image: Any, core_version: str, uv_version: str
) -> None:
    core_image.distribution.version = core_version
    core_image.uv = uv_version
    core_image.qualify()
    command = core_image.run.call_args_list[0].args[0]
    assert f"homeassistant=={core_version}" in command
    assert f"uv=={uv_version}" in command
    assert command[command.index("--constraint") + 1] == str(core_image.constraints)
    proof = json.loads(core_image.output.read_text())
    assert proof["core_version"] == core_version
    assert proof["uv_version"] == uv_version


def test_core_baseline_contains_only_its_exact_frontend_not_ha_mcp_dependencies(
    core_image: Any,
) -> None:
    core_image.qualify()
    bootstrap = core_image.run.call_args_list[0].args[0]
    assert "home-assistant-frontend==20260930.5" in bootstrap
    assert not any("joserfc" in arg or "ha-mcp" in arg for arg in bootstrap)
    proof = json.loads(core_image.output.read_text())
    assert proof["bootstrap_frontend"] == "home-assistant-frontend==20260930.5"


@pytest.mark.parametrize(
    "requirements", [["unrelated==1.0"], ["home-assistant-frontend>=1.0"], []]
)
def test_core_baseline_rejects_unexpected_frontend_manifest(
    core_image: Any,
    requirements: list[str],
) -> None:
    path = core_image.constraints.parent / "components/frontend/manifest.json"
    path.write_text(json.dumps({"requirements": requirements}))
    with pytest.raises(ValueError, match="frontend"):
        core_image.qualify()
    core_image.run.assert_not_called()


def test_minimal_lane_rejects_bootstrap_constraints_from_another_core(
    core_image: Any, tmp_path: Path
) -> None:
    core_image.distribution.locate_file = lambda _filename: tmp_path / "other-core"
    with pytest.raises(RuntimeError, match=r"constraints.*installed Core"):
        core_image.qualify()
    core_image.run.assert_not_called()


def test_minimal_lane_accepts_constraints_owned_by_the_editable_core_image(
    core_image: Any, tmp_path: Path
) -> None:
    core_image.distribution.locate_file = lambda _filename: tmp_path / "site-packages"
    core_image.distribution.read_text = lambda _filename: json.dumps(
        {
            "url": core_image.constraints.parent.parent.as_uri(),
            "dir_info": {"editable": True},
        }
    )
    core_image.qualify()
    assert core_image.run.call_count == 3


def test_minimal_lane_records_the_interpreter_supplied_by_the_image(
    core_image: Any,
) -> None:
    core_image.qualify()
    proof = json.loads(core_image.output.read_text())
    assert proof["interpreter"]["executable"] == sys.executable
    assert proof["interpreter"]["version"] == sys.version
    assert proof["interpreter"]["architecture"] == platform.machine()


def test_missing_runtime_receipt_stops_before_bootstrap(
    core_image: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        minimal_qualification,
        "attest_runtime",
        Mock(side_effect=FileNotFoundError("image receipt")),
    )
    with pytest.raises(FileNotFoundError, match="receipt"):
        core_image.qualify()
    core_image.run.assert_not_called()
    assert not json.loads(core_image.output.read_text())["complete"]


def test_security_exception_reaches_native_install_but_not_old_certifi_bootstrap(
    core_image: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "7 days")
    monkeypatch.delenv("UV_CONFIG_FILE", raising=False)
    monkeypatch.delenv("UV_NO_CONFIG", raising=False)
    core_image.qualify()
    bootstrap, seed, native = core_image.run.call_args_list
    assert "UV_CONFIG_FILE" not in bootstrap.kwargs["env"]
    assert "certifi==2026.7.22" in seed.args[0]
    assert "UV_CONFIG_FILE" not in seed.kwargs["env"]
    assert Path(native.kwargs["env"]["UV_CONFIG_FILE"]).is_file()
    assert native.kwargs["env"]["UV_EXCLUDE_NEWER"] == "7 days"
    proof = json.loads(core_image.output.read_text())
    assert proof["native_install_policy"]["exception_active"]


def test_core_worker_does_not_discover_checkout_uv_policy(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[tool.uv]\nexclude-newer = "7 days"\n')
    work = tmp_path / "isolated-core"
    work.mkdir()
    monkeypatch.setattr(minimal_qualification, "ROOT", checkout)
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "7 days")
    env = minimal_qualification.isolated_environment(work / "venv")
    probe = """
import os
from pathlib import Path

current = Path.cwd()
assert not any((parent / 'pyproject.toml').exists() for parent in (current, *current.parents)), 'Core inherited the checkout policy'
assert os.environ['UV_EXCLUDE_NEWER'] == '7 days', 'Explicit user policy was removed'
print('Core process has its own working directory and keeps explicit user policy')
"""
    log = tmp_path / "worker.log"

    minimal_qualification.run_logged([sys.executable, "-c", probe], env=env, log=log)

    assert "keeps explicit user policy" in log.read_text()


@pytest.fixture
def minimal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    baseline = {
        "versions": {"homeassistant": "2026.10.2", "certifi": "2026.7.22"},
        "issues": [],
    }
    dependency = "ha-mcp @ https://github.com/fixture/paired.whl#sha256=fixture"
    requirements = ["starlette>=1", dependency, "pydantic>=2"]
    wheel = tmp_path / "paired.whl"
    output = tmp_path / "proof.json"
    output.write_text(json.dumps({"complete": False, "old_certifi_seed": "2026.7.22"}))
    world = SimpleNamespace(
        changed=False,
        conflict=False,
        output=output,
        wheel=wheel,
        requirements=requirements,
        events=[],
        additions={},
        constraints=tmp_path / "package_constraints.txt",
    )
    world.constraints.write_text("homeassistant==2026.10.2\ncertifi>=2021.5.30\n")

    def prepare(_assets: Path, component: Path, *, repository: str) -> Any:
        component.mkdir(parents=True)
        (component / "manifest.json").write_text(
            json.dumps(
                {
                    "requirements": requirements,
                    "dependencies": ["webhook"],
                    "after_dependencies": ["frontend", "webhook"],
                }
            )
        )
        return (
            {"revision": "source", "files": {wheel.name: "hash"}},
            {"wheel": wheel.name, "dependency": dependency},
            wheel,
            "manifest-hash",
        )

    def snapshot() -> dict[str, Any]:
        current = copy.deepcopy(baseline)
        current["versions"].update(world.additions)
        if world.changed:
            current["versions"]["certifi"] = "unexpected-upgrade"
        if world.conflict:
            current["issues"] = ["new incompatible package"]
        return current

    def install_package(
        _requirement: str, *, upgrade: bool = True, **kwargs: Any
    ) -> bool:
        assert upgrade is True and kwargs == {"constraints": str(world.constraints)}
        return True

    def requirements_owner(values: list[str], kwargs: dict[str, Any]) -> Any:
        assert kwargs == {"constraints": str(world.constraints)}
        world.events.append("direct")
        return set(values), set()

    async def dependency_owner(hass: Any, domain: str) -> Any:
        assert hass is world.hass
        world.loader_setup.assert_called_once_with(hass)
        world.events.append(domain)
        world.additions[f"dependency-{domain}"] = "1"
        return SimpleNamespace(domain=domain)

    world.owner = Mock(side_effect=requirements_owner)
    world.hass = SimpleNamespace(async_stop=AsyncMock())
    world.loader_setup = Mock()
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(async_setup=world.loader_setup),
    )
    world.dependency_owner = AsyncMock(side_effect=dependency_owner)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.core",
        SimpleNamespace(HomeAssistant=lambda _config: world.hass),
    )
    world.package = SimpleNamespace(
        install_package=Mock(side_effect=install_package),
        is_installed=Mock(return_value=False),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.requirements",
        SimpleNamespace(
            pip_kwargs=lambda _config: {"constraints": str(world.constraints)},
            _install_requirements_if_missing=world.owner,
            async_get_integration_with_requirements=world.dependency_owner,
        ),
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.util", SimpleNamespace(package=world.package)
    )
    monkeypatch.setattr(minimal_install, "assert_isolated", lambda: None)
    monkeypatch.setattr(minimal_install, "prepare_component", prepare)
    monkeypatch.setattr(minimal_install, "environment", snapshot)
    world.probes = AsyncMock(return_value=["unit-test-probe"])
    monkeypatch.setattr(minimal_probes, "run_probes", world.probes)
    world.run = lambda: minimal_install.install(tmp_path, tmp_path, output)
    return world


def test_every_manifest_requirement_uses_core_owner_and_only_paired_url_is_substituted(
    minimal: Any,
) -> None:
    minimal.run()
    sent = [call.args[0][0] for call in minimal.owner.call_args_list]
    assert sent == [
        minimal.requirements[0],
        f"ha-mcp @ {minimal.wheel.resolve().as_uri()}",
        minimal.requirements[2],
    ]
    assert "upgrade" not in minimal.package.install_package.call_args.kwargs
    proof = json.loads(minimal.output.read_text())
    assert proof["complete"]
    assert [row["paired_local_substitution"] for row in proof["decisions"]] == [
        False,
        True,
        False,
    ]
    assert (
        "pydantic" not in proof["before"]["versions"]
        and "mcp" not in proof["before"]["versions"]
    )
    assert proof["observations"][-1]["phase"] == "after_runtime_probes"


@pytest.mark.parametrize("kind", ["changed", "conflict"])
def test_manifest_phase_failure_preserves_all_dependency_evidence(
    minimal: Any, kind: str
) -> None:
    original = minimal.owner.side_effect

    def apply(values: list[str], kwargs: dict[str, Any]) -> Any:
        if values == [minimal.requirements[-1]]:
            setattr(minimal, kind, True)
        return original(values, kwargs)

    minimal.owner.side_effect = apply
    with pytest.raises(RuntimeError, match="dependency policy or added conflicts"):
        minimal.run()
    proof = json.loads(minimal.output.read_text())
    assert not proof["complete"] and len(proof["decisions"]) == len(
        minimal.requirements
    )
    failure = proof["failure_environment"]
    assert (
        failure["changed_preinstalled"] if kind == "changed" else failure["new_issues"]
    )
    minimal.probes.assert_not_awaited()


def test_core_skips_already_satisfied_roots_instead_of_force_upgrading_them(
    minimal: Any,
) -> None:
    minimal.package.is_installed.return_value = True
    kwargs = {"constraints": str(minimal.constraints)}
    assert (
        minimal_install.install_requirement("starlette>=1", kwargs)
        == "already_satisfied"
    )
    minimal.package.install_package.assert_not_called()
    minimal.owner.assert_called_once_with(["starlette>=1"], kwargs)


def test_declared_dependencies_use_core_owners_after_direct_requirements(
    minimal: Any,
) -> None:
    async def probe(_config: Path) -> list[str]:
        minimal.events.append("probes")
        return []

    minimal.probes.side_effect = probe
    minimal.run()
    assert minimal.events == [
        "direct",
        "direct",
        "direct",
        "webhook",
        "frontend",
        "probes",
    ]
    proof = json.loads(minimal.output.read_text())
    phases = [row["phase"] for row in proof["observations"]]
    assert phases[-3:] == [
        "manifest_dependency:webhook",
        "manifest_after_dependency:frontend",
        "after_runtime_probes",
    ]
    assert "dependency-frontend" in proof["observations"][-1]["versions"]
    minimal.hass.async_stop.assert_awaited_once_with(force=True)


def test_minimal_manifest_accepts_a_compatible_upgrade_and_retains_both_versions(
    minimal: Any,
) -> None:
    original = minimal.owner.side_effect

    def upgrade(values: list[str], kwargs: dict[str, Any]) -> Any:
        result = original(values, kwargs)
        minimal.additions["certifi"] = "2026.10.19"
        return result

    minimal.owner.side_effect = upgrade
    minimal.run()
    proof = json.loads(minimal.output.read_text())
    assert proof["complete"]
    assert proof["observations"][-1]["compatible_upgrades"]["certifi"] == {
        "before": "2026.7.22",
        "after": "2026.10.19",
    }


def test_dependency_free_wheel_cannot_upgrade_core_before_manifest_processing(
    minimal: Any,
) -> None:
    def wheel_install(*_args: Any, **_kwargs: Any) -> bool:
        minimal.additions["certifi"] = "2026.10.19"
        return True

    minimal.package.install_package.side_effect = wheel_install
    with pytest.raises(RuntimeError, match="dependency policy"):
        minimal.run()
    proof = json.loads(minimal.output.read_text())
    observation = proof["observations"][-1]
    assert observation["phase"] == "paired_wheel" and not observation["verified"]
    assert any(
        error["reason"] == "paired_wheel_changed_preinstalled"
        for error in observation["policy_errors"]
    )
    minimal.owner.assert_not_called()


def test_minimal_subsequent_phase_cannot_downgrade_a_new_dependency(
    minimal: Any,
) -> None:
    original = minimal.owner.side_effect

    def install(values: list[str], kwargs: dict[str, Any]) -> Any:
        result = original(values, kwargs)
        minimal.additions["new-library"] = "2.0" if len(minimal.events) == 1 else "1.0"
        return result

    minimal.owner.side_effect = install
    with pytest.raises(RuntimeError, match="dependency policy"):
        minimal.run()
    proof = json.loads(minimal.output.read_text())
    assert not proof["complete"]
    assert any(
        row["reason"] == "downgrade"
        for row in proof["observations"][-1]["rejected_changes"]
    )


def test_after_dependency_cannot_hide_a_shared_core_upgrade(minimal: Any) -> None:
    original = minimal.dependency_owner.side_effect

    async def upgrade(hass: Any, domain: str) -> Any:
        result = await original(hass, domain)
        if domain == "frontend":
            minimal.changed = True
        return result

    minimal.dependency_owner.side_effect = upgrade
    with pytest.raises(RuntimeError, match="dependency policy or added conflicts"):
        minimal.run()
    proof = json.loads(minimal.output.read_text())
    assert not proof["complete"]
    assert proof["observations"][-1]["phase"] == "manifest_after_dependency:frontend"
    assert (
        proof["observations"][-1]["changed_preinstalled"]["certifi"]["before"]
        == "2026.7.22"
    )
    minimal.probes.assert_not_awaited()
    minimal.hass.async_stop.assert_awaited_once_with(force=True)


def test_new_packages_are_allowed_but_disappearing_preinstalled_packages_fail(
    tmp_path: Path,
) -> None:
    constraints = tmp_path / "package_constraints.txt"
    constraints.write_text("")
    before = {"versions": {"Core-owned": "1"}, "issues": ["known"]}
    assert changes(
        before,
        {"versions": {"Core-owned": "1", "new": "2"}, "issues": []},
        constraint_path=constraints,
    )["verified"]
    assert (
        changes(before, {"versions": {}, "issues": []}, constraint_path=constraints)[
            "changed_preinstalled"
        ]["core-owned"]["after"]
        is None
    )


def test_environment_does_not_inherit_host_interpreter_or_source_package_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in (
        "PYTHONHOME",
        "UV_PYTHON",
        "UV_SYSTEM_PYTHON",
        "UV_PROJECT_ENVIRONMENT",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "host-environment")
    result = minimal_qualification.isolated_environment(tmp_path)
    assert "host-environment" not in result.values()
    assert result["PYTHONNOUSERSITE"] == "1"
    assert result["VIRTUAL_ENV"] == str(tmp_path)


def test_sanitized_log_keeps_origin_but_not_url_credentials_or_signed_parameters() -> (
    None
):
    value = sanitize(
        "Fetch https://user:secret@assets.example.test/wheel?signature=private#fragment"
    )
    assert value == "Fetch https://assets.example.test/wheel"


async def test_real_private_sdk_http_client_exercises_lazy_state_store() -> None:
    from ._embedded_stubs import install

    install()
    await minimal_probes._legacy_http()


@pytest.fixture
def native_base(minimal: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    entries = object()
    minimal.entries_owner = Mock(return_value=entries)
    minimal.base_owner = AsyncMock(return_value=True)
    minimal.custom_components = AsyncMock(return_value={})
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(ConfigEntries=minimal.entries_owner),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.bootstrap",
        SimpleNamespace(async_load_base_functionality=minimal.base_owner),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(
            async_setup=minimal.loader_setup,
            async_get_custom_components=minimal.custom_components,
        ),
    )
    return minimal


async def test_native_probe_loads_core_base_owners_before_authentication(
    native_base: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class InitializedCoreReached(Exception):
        pass

    async def auth(hass: Any, _providers: Any, _modules: Any) -> Any:
        native_base.loader_setup.assert_called_once_with(hass)
        native_base.entries_owner.assert_called_once_with(hass, {})
        assert hass.config_entries is native_base.entries_owner.return_value
        native_base.custom_components.assert_awaited_once_with(hass)
        native_base.base_owner.assert_awaited_once_with(hass)
        raise InitializedCoreReached

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.auth",
        SimpleNamespace(auth_manager_from_config=auth),
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.auth.const", SimpleNamespace(GROUP_ID_ADMIN="admin")
    )
    with pytest.raises(InitializedCoreReached):
        await minimal_probes._native(tmp_path)
    native_base.hass.async_stop.assert_awaited_once_with(force=True)


async def test_failed_core_base_bootstrap_stops_before_creating_an_actor(
    native_base: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native_base.base_owner.return_value = False
    auth = AsyncMock(
        side_effect=AssertionError("Auth must not run without Core owners")
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.auth",
        SimpleNamespace(auth_manager_from_config=auth),
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.auth.const", SimpleNamespace(GROUP_ID_ADMIN="admin")
    )
    with pytest.raises(RuntimeError, match="base functionality"):
        await minimal_probes._native(tmp_path)
    auth.assert_not_awaited()
    native_base.hass.async_stop.assert_awaited_once_with(force=True)
