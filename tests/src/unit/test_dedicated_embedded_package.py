"""Dedicated boots must let Core install the library paired with their component."""

import json

import pytest

from tests.src.e2e.workflows.embedded import test_embedded_server as bootstrap
from tests.src.e2e.workflows.embedded import test_embedded_update_path as update


@pytest.fixture
def sources(tmp_path, monkeypatch):
    initial = tmp_path / "initial"
    (initial / ".storage").mkdir(parents=True)
    (initial / ".storage/core.config_entries").write_text(
        json.dumps({"data": {"entries": []}})
    )
    component = tmp_path / "source"
    component.mkdir()
    manifest = {"requirements": ["mcp>=1.24.0", "ha-mcp==8.7.0", "ruamel.yaml"]}
    (component / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(bootstrap, "_INITIAL_STATE", initial)
    monkeypatch.setattr(bootstrap, "_INTEGRATION_SRC", component)
    monkeypatch.setattr(update, "_INITIAL_STATE", initial)
    return component, manifest


@pytest.mark.parametrize("scenario", ["bootstrap", "candidate_update", "stable_update"])
def test_core_requirement_matches_the_dedicated_boot_generation(
    tmp_path, sources, scenario
):
    component, original = sources
    config = tmp_path / "config"
    wheel = "ha_mcp-8.7.0.post998-py3-none-any.whl"
    requirement = f"ha-mcp @ file:///config/{wheel}"
    if scenario == "bootstrap":
        bootstrap._seed_config(config, wheel)
    elif scenario == "candidate_update":
        update._seed_config(config, component, initial_wheel_name=wheel)
    else:
        update._seed_config(config, component)

    manifest = json.loads(
        (config / "custom_components/ha_mcp_tools/manifest.json").read_text()
    )
    entries = json.loads((config / ".storage/core.config_entries").read_text())["data"][
        "entries"
    ]
    server = next(
        entry for entry in entries if entry["unique_id"] == "ha_mcp_tools-server"
    )
    if scenario == "stable_update":
        assert manifest == original
        assert server["options"]["pip_spec"] == ""
    else:
        assert manifest["requirements"] == ["mcp>=1.24.0", requirement, "ruamel.yaml"]
        assert server["options"]["pip_spec"] == requirement
    assert json.loads((component / "manifest.json").read_text()) == original
