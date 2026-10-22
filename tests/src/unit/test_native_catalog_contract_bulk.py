"""Composed native tools preserve their existing client contracts."""

import json
from pathlib import Path
from types import SimpleNamespace

from .test_native_catalog_contract import (
    _ANNOTATIONS,
    _changed_inputs,
    _structural_schema,
)
from .test_native_engine_backend import native as native


def test_composed_bulk_keeps_schemas_tags_and_annotations(native: object) -> None:
    from custom_components.ha_mcp_tools.native_catalog import build_native_catalog

    manager = SimpleNamespace(
        backend=SimpleNamespace(_templates=None, hass=None, authorize=None),
        hass=None,
        snapshots=None,
    )
    actual = {tool.name: tool for tool in build_native_catalog(manager)}
    baseline = {
        row["name"]: row
        for row in json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "docs/native-migration/catalog-baseline.json"
            ).read_text(encoding="utf-8")
        )["tools"]
    }
    for name in (
        "ha_bulk_control",
        "ha_get_operation_status",
        "ha_config_list_groups",
        "ha_config_set_group",
        "ha_config_remove_group",
        "ha_manage_blueprints",
        "ha_search",
        "ha_manage_energy_prefs",
        "ha_manage_pipeline",
        "ha_manage_updates",
        "ha_manage_radio",
        "ha_list_files",
        "ha_read_file",
        "ha_write_file",
        "ha_delete_file",
        "ha_config_get_yaml",
        "ha_config_set_yaml",
        "ha_get_hacs_info",
        "ha_manage_hacs",
        "ha_restart",
        "ha_reload_core",
        "ha_manage_theme",
    ):
        definition = actual[name]
        old = baseline[name]
        assert not _changed_inputs(old["inputSchema"], definition.input_schema)
        assert _structural_schema(old.get("outputSchema")) == _structural_schema(
            definition.output_schema
        )
        assert {key: definition.annotations.get(key) for key in _ANNOTATIONS} == {
            key: old["annotations"].get(key) for key in _ANNOTATIONS
        }
        assert definition.tags == frozenset(old["_meta"]["fastmcp"]["tags"])
