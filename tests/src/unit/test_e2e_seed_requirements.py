"""E2E component libraries and embedded servers must come from the checkout.

The component manifest pins the released ``ha-mcp``; installing that pin before
the wheel built from the checkout would leave the released code under test,
because pip then treats the same-version wheel as already satisfied.
"""

from __future__ import annotations

import json

import pytest

from tests.src.e2e._conftest_seed import (
    _collect_manifest_requirements,
    is_server_requirement,
)


@pytest.mark.parametrize(
    "requirement",
    ["ha-mcp==8.6.0", "ha_mcp-dev==9.0.0.dev2901", "HA-MCP>=8", " ha-mcp"],
)
def test_server_pins_are_recognised(requirement: str) -> None:
    assert is_server_requirement(requirement)


@pytest.mark.parametrize(
    "requirement", ["mcp>=1.24.0", "ha-mcp-tools==1", "ruamel.yaml>=0.18.0"]
)
def test_other_requirements_are_not_server_pins(requirement: str) -> None:
    assert not is_server_requirement(requirement)


def _config(tmp_path, requirements: list[str]):
    component = tmp_path / "custom_components" / "ha_mcp_tools"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(json.dumps({"requirements": requirements}))
    return tmp_path


def test_a_checkout_wheel_replaces_only_the_manifest_server_pin(tmp_path) -> None:
    config = _config(tmp_path, ["mcp>=1.24.0", "ha-mcp==8.6.0", "ruamel.yaml"])
    assert _collect_manifest_requirements(config, skip_server=True) == [
        "mcp>=1.24.0",
        "ruamel.yaml",
    ]


def test_manifest_pin_is_retained_without_a_checkout_wheel(tmp_path) -> None:
    config = _config(tmp_path, ["mcp>=1.24.0", "ha-mcp==8.6.0"])
    assert _collect_manifest_requirements(config) == ["mcp>=1.24.0", "ha-mcp==8.6.0"]
