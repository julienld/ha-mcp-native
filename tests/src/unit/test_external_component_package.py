"""An external server still needs matching component-side library code in HA."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.src.e2e import _conftest_testcontainer as harness


@pytest.mark.parametrize("no_tools", [False, True])
def test_external_component_boot_uses_checkout_library_without_a_server_entry(
    tmp_path, monkeypatch, no_tools
):
    repo = tmp_path / "repo"
    initial = repo / "tests/initial_test_state"
    (initial / ".storage").mkdir(parents=True)
    (initial / ".storage/core.config_entries").write_text(
        json.dumps({"data": {"entries": []}})
    )
    source = repo / "custom_components/ha_mcp_tools"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps({"requirements": ["ha-mcp==0.0.1", "ruamel.yaml"]})
    )
    monkeypatch.setattr(
        harness, "__file__", str(repo / "tests/src/e2e/_conftest_testcontainer.py")
    )
    monkeypatch.setattr(
        harness.tempfile, "mkdtemp", lambda **_: str(tmp_path / "config")
    )
    monkeypatch.setattr(harness, "_is_no_tools_entry_selected", lambda: no_tools)
    monkeypatch.setattr(harness, "native_runtime_selected", lambda: False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("HA_TEST_PORT", raising=False)
    for name in (
        "_ensure_hacs_frontend",
        "_seed_legacy_yaml_backups",
        "_seed_non_yaml_package_file",
        "_seed_yaml_package_scene",
        "_refresh_recorder_timestamps",
        "_setup_config_permissions",
    ):
        monkeypatch.setattr(harness, name, lambda *_: None)
    monkeypatch.setattr(harness, "_copy_local_blueprint_to_www", lambda *_: {})

    def build(destination: Path) -> Path:
        wheel = destination / "ha_mcp-0.0.1-py3-none-any.whl"
        wheel.write_bytes(b"checkout package")
        return wheel

    builder = Mock(side_effect=build)
    monkeypatch.setattr(harness, "_build_embedded_server_wheel", builder)
    server_entry = Mock(side_effect=AssertionError("The server must remain external"))
    monkeypatch.setattr(harness, "_install_embedded_server", server_entry)
    container = Mock()
    for method in (
        "with_exposed_ports",
        "with_volume_mapping",
        "with_env",
        "with_kwargs",
    ):
        getattr(container, method).return_value = container
    monkeypatch.setattr(harness, "DockerContainer", lambda *_: container)

    config, _, _, wheel_name = harness._prepare_testcontainer_config(embedded=False)
    entries = json.loads((config / ".storage/core.config_entries").read_text())["data"][
        "entries"
    ]
    if no_tools:
        assert wheel_name is None and entries == []
        assert not (config / "custom_components/ha_mcp_tools").exists()
        builder.assert_not_called()
    else:
        assert wheel_name is not None, (
            "HA received component files without their library"
        )
        assert (config / wheel_name).read_bytes() == b"checkout package"
        assert [entry["unique_id"] for entry in entries] == ["ha_mcp_tools"]
        harness._build_ha_testcontainer(config, False, wheel_name)
        command = container.with_kwargs.call_args.kwargs["entrypoint"][-1]
        assert f"/config/{wheel_name}" in command
        assert "--constraint" in command
        assert "ha-mcp==0.0.1" not in command
    server_entry.assert_not_called()
