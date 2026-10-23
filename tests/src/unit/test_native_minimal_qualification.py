"""The minimal lane must expose changes hidden by a preinstalled Core image."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from tests.native import minimal_install, minimal_probes, minimal_qualification
from tests.native.minimal_support import changes, sanitize


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
    )

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
        assert upgrade is True and kwargs == {"constraints": "Core-owned-constraints"}
        return True

    def requirements_owner(values: list[str], kwargs: dict[str, Any]) -> Any:
        assert kwargs == {"constraints": "Core-owned-constraints"}
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
            pip_kwargs=lambda _config: {"constraints": "Core-owned-constraints"},
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
    with pytest.raises(RuntimeError, match="baseline or added conflicts"):
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
    kwargs = {"constraints": "Core-owned-constraints"}
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


def test_after_dependency_cannot_hide_a_shared_core_upgrade(minimal: Any) -> None:
    original = minimal.dependency_owner.side_effect

    async def upgrade(hass: Any, domain: str) -> Any:
        result = await original(hass, domain)
        if domain == "frontend":
            minimal.changed = True
        return result

    minimal.dependency_owner.side_effect = upgrade
    with pytest.raises(RuntimeError, match="baseline or added conflicts"):
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


def test_new_packages_are_allowed_but_disappearing_preinstalled_packages_fail() -> None:
    before = {"versions": {"Core-owned": "1"}, "issues": ["known"]}
    assert not any(
        changes(
            before, {"versions": {"Core-owned": "1", "new": "2"}, "issues": []}
        ).values()
    )
    assert (
        changes(before, {"versions": {}, "issues": []})["changed_preinstalled"][
            "Core-owned"
        ]["after"]
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
