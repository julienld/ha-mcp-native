"""Resource bindings retain the original public schema and documented native effect."""

import json
from pathlib import Path

from .test_native_catalog_contract import _ANNOTATIONS, _changed_inputs
from .test_native_engine_backend import native as native
from .test_native_resource_service import resources as resources


def test_composed_resources_preserve_schema_tags_and_explicit_native_annotations(
    resources,
):
    from custom_components.ha_mcp_tools.native_catalog import build_native_catalog

    manager, engine, *_ = resources
    manager.backend._templates = None
    manager.backend.hass = manager.hass
    baseline = {
        row["name"]: row
        for row in json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "docs/native-migration/catalog-baseline.json"
            ).read_text(encoding="utf-8")
        )["tools"]
    }
    # The composed catalog must select the actual resource bindings, not only
    # a signature-only definition which unit callers constructed separately.
    actual = {
        definition.name: definition for definition in build_native_catalog(manager)
    }
    for name in engine.tools:
        definition = actual[name]
        old = baseline[name]
        assert not _changed_inputs(old["inputSchema"], definition.input_schema)
        expected = dict(old["annotations"])
        if name == "ha_config_list_dashboard_resources":
            expected.update(
                readOnlyHint=False, destructiveHint=True, idempotentHint=False
            )
            assert definition.writes is not None
            assert "migrate" in definition.meta["ha_mcp"]["native_support"]["note"]
        assert {key: definition.annotations.get(key) for key in _ANNOTATIONS} == {
            key: expected.get(key) for key in _ANNOTATIONS
        }
        assert definition.tags == frozenset(old["_meta"]["fastmcp"]["tags"])
