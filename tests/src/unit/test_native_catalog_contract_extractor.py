"""Moving tool methods to the common engine must not erase public documentation."""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]


def test_shared_method_reexports_keep_their_original_catalog_metadata() -> None:
    """Direct and inherited shared methods retain schemas, prose and hints."""
    spec = importlib.util.spec_from_file_location(
        "native_contract_extractor", _ROOT / "scripts" / "extract_tools.py"
    )
    assert spec is not None and spec.loader is not None
    extractor = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = extractor
    spec.loader.exec_module(extractor)
    previous = {
        tool["name"]: tool
        for tool in json.loads(
            (_ROOT / "site" / "src" / "data" / "tools.json").read_text(encoding="utf-8")
        )
    }
    current = {tool["name"]: tool for tool in extractor.extract_tools()}
    migrated = (
        "ha_get_state",
        "ha_list_services",
        "ha_list_floors_areas",
        "ha_manage_custom_tool",
    )
    missing = [name for name in migrated if name not in current]
    assert not missing, (
        f"Shared tool reexports disappeared from the public catalog: {missing}"
    )
    for name in migrated:
        assert current[name] == previous[name], (
            f"Shared method metadata changed: {name}"
        )


def test_inventory_recognizes_literal_batches_in_native_catalog() -> None:
    from ha_mcp.runtime.catalog import build_initial_catalog

    script = runpy.run_path(str(_ROOT / "scripts" / "native_inventory.py"))
    inventory = script["build_inventory"]()
    public = {tool["name"] for tool in inventory["tools"]}
    actual = {
        definition.name for definition in build_initial_catalog(object())
    } & public
    scanned = {
        tool["name"]
        for tool in inventory["tools"]
        if tool["native_catalog_declaration"]
    }
    assert scanned == actual
    assert inventory["counts"]["native_catalog_declarations"] == len(actual)
