"""Core must preflight host requirements individually, outside the wheel graph."""

import json
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

from .test_native_release import release_builder as release_builder


def project(tmp_path):
    source = Path(__file__).resolve().parents[3] / "pyproject.toml"
    target = tmp_path / "pyproject.toml"
    target.write_bytes(source.read_bytes())
    return target


def test_native_wheel_has_no_active_dependency_edges_while_standalone_keeps_all(
    tmp_path, release_builder
):
    path = project(tmp_path)
    original = tomllib.loads(path.read_text())["project"]["dependencies"]
    release_builder.select_native_dependencies(path)
    prepared = tomllib.loads(path.read_text())["project"]
    assert prepared["dependencies"] == []
    assert prepared["optional-dependencies"]["standalone"] == original


def test_native_manifest_does_not_reactivate_optional_extra_graphs(
    tmp_path, release_builder
):
    requirements = release_builder.select_native_dependencies(project(tmp_path))
    parsed = {
        Requirement(value).name.lower().replace("_", "-"): Requirement(value)
        for value in requirements
    }
    assert not any(requirement.extras for requirement in parsed.values())
    assert {
        "pydantic",
        "pydantic-settings",
        "httpx2",
        "starlette",
        "uvicorn",
        "socksio",
        "email-validator",
        "py-key-value-aio",
        "cachetools",
    } <= parsed.keys()
    assert (
        not {
            "mcp",
            "authlib",
            "joserfc",
            "httpx",
            "cryptography",
            "pyjwt",
            "anyio",
            "pyyaml",
            "rich",
            "openapi-pydantic",
            "jsonschema-path",
        }
        & parsed.keys()
    )


def test_published_manifest_removes_cores_shared_mcp_and_adds_individual_requirements(
    tmp_path, release_builder
):
    component = tmp_path / "component"
    component.mkdir()
    (component / "manifest.json").write_text(
        json.dumps(
            {
                "domain": "ha_mcp_tools",
                "requirements": ["mcp>=1.24.0", "ha-mcp==8.7.0", "ruamel.yaml>=0.18.0"],
            }
        )
    )
    (component / "const.py").write_text('COMPONENT_VERSION = "8.7.0"\n')
    release_builder.stamp_component(
        component,
        "9.0.0.dev3",
        "ha-mcp @ https://example.invalid/ha_mcp-9.0.0.dev3-py3-none-any.whl",
        "julienld/ha-mcp-native",
        native_requirements=["starlette>=1.0.1", "cachetools>=5"],
    )
    requirements = json.loads((component / "manifest.json").read_text())["requirements"]
    assert not any(Requirement(value).name == "mcp" for value in requirements)
    assert {"starlette>=1.0.1", "cachetools>=5", "ruamel.yaml>=0.18.0"} <= set(
        requirements
    )
