"""Local build policy tests; Core installation itself is qualified on the runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "hacs_image_dependencies", Path(__file__).with_name("image_dependencies.py")
)
assert spec and spec.loader
image_dependencies = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_dependencies)


@pytest.fixture
def build(tmp_path: Path, monkeypatch: Any) -> SimpleNamespace:
    requirements = {
        "hacs": ["aiogithubapi>=22.10.1"],
        "ha_mcp_tools": ["ruamel.yaml>=0.18.0", "ha-mcp==9.0.0.dev83"],
    }
    for domain, deps in requirements.items():
        path = tmp_path / domain / "manifest.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"requirements": deps}), encoding="utf-8")
    satisfied = {"aiogithubapi>=22.10.1", "ha-mcp==9.0.0.dev83"}

    def install(requirement: str, **kwargs: Any) -> bool:
        satisfied.add(requirement)
        return True

    package = SimpleNamespace(
        is_installed=satisfied.__contains__, install_package=Mock(side_effect=install)
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.util", SimpleNamespace(package=package)
    )
    return SimpleNamespace(
        components=tmp_path,
        constraints=tmp_path / "core-constraints.txt",
        package=package,
        satisfied=satisfied,
    )


def test_build_installs_missing_manifest_dependency_before_network_is_removed(
    build: SimpleNamespace,
) -> None:
    image_dependencies.install_manifest_requirements(
        build.components, build.constraints
    )
    # Core and the paired wheel are already satisfied; installing them again
    # would allow unnecessary shared-package upgrades.
    build.package.install_package.assert_called_once_with(
        "ruamel.yaml>=0.18.0", constraints=str(build.constraints)
    )
    assert "ruamel.yaml>=0.18.0" in build.satisfied


def test_build_stops_when_core_cannot_install_a_requirement(
    build: SimpleNamespace,
) -> None:
    build.package.install_package.side_effect = None
    build.package.install_package.return_value = False
    with pytest.raises(RuntimeError, match=r"Build could not install ruamel\.yaml"):
        image_dependencies.install_manifest_requirements(
            build.components, build.constraints
        )


def test_build_rechecks_earlier_requirements_after_installs(
    build: SimpleNamespace,
) -> None:
    def incompatible_install(requirement: str, **kwargs: Any) -> bool:
        build.satisfied.add(requirement)
        build.satisfied.remove("aiogithubapi>=22.10.1")
        return True

    build.package.install_package.side_effect = incompatible_install
    with pytest.raises(
        RuntimeError, match=r"Unsatisfied runtime requirements.*aiogithubapi"
    ):
        image_dependencies.install_manifest_requirements(
            build.components, build.constraints
        )
