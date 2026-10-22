"""Existing MCP callers keep their tool and argument contracts during migration.

The baseline was captured from upstream, not built from the implementation under
test. New tools and optional parameters may be added without recapturing it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from .test_container_param_coercion_complete import _all_registered_tools

_CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "native-migration"
_ANNOTATIONS = (
    "title",
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)
_DOCUMENTATION = frozenset({"title", "description", "examples", "$comment"})
_NAME_MAPS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)
_OPAQUE_VALUES = frozenset({"default", "const", "enum", "example", "examples"})


def _structural_schema(value: Any) -> Any:
    """Ignore prose, preserving user keys and literal values named like prose."""
    if isinstance(value, list):
        return [_structural_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in _DOCUMENTATION:
            continue
        if key in _NAME_MAPS and isinstance(item, dict):
            result[key] = {
                name: _structural_schema(schema) for name, schema in item.items()
            }
        elif key in _OPAQUE_VALUES or key.startswith("x-"):
            result[key] = item
        else:
            result[key] = _structural_schema(item)
    return result


def _changed_inputs(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changes = []
    old_properties = before.get("properties", {})
    new_properties = after.get("properties", {})
    for name, schema in old_properties.items():
        if name not in new_properties:
            changes.append(f"removed parameter {name}")
        elif _structural_schema(schema) != _structural_schema(new_properties[name]):
            changes.append(f"changed parameter {name}")
    if set(after.get("required", [])) - set(before.get("required", [])):
        changes.append("newly required parameters")
    changes.extend(
        f"changed root {key}"
        for key in ("$defs", "definitions", "additionalProperties", "type")
        if _structural_schema(before.get(key)) != _structural_schema(after.get(key))
    )
    return changes


def test_native_migration_keeps_preexisting_mcp_call_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reused tool name cannot silently stop accepting the old arguments."""
    monkeypatch.setenv("HAMCP_ENABLE_DEV_MODE", "true")
    registered = _all_registered_tools()
    baseline = json.loads(
        (_CONTRACTS / "catalog-baseline.json").read_text(encoding="utf-8")
    )
    changes = {}
    for old in baseline["tools"]:
        name = old["name"]
        if name not in registered:
            changes[name] = ["tool no longer registered"]
            continue
        current = (
            registered[name]
            .to_mcp_tool()
            .model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        differences = _changed_inputs(old["inputSchema"], current["inputSchema"])
        for hint in _ANNOTATIONS:
            if old["annotations"].get(hint) != current.get("annotations", {}).get(hint):
                differences.append(f"changed safety annotation {hint}")
        if _structural_schema(old.get("outputSchema")) != _structural_schema(
            current.get("outputSchema")
        ):
            differences.append("changed advertised output schema")
        if differences:
            changes[name] = differences
    assert not changes, f"Native migration broke baseline MCP contracts: {changes}"


def test_native_migration_keeps_cached_retired_tool_names_callable() -> None:
    """Older clients retain aliases after the embedded transport is replaced."""
    from ha_mcp.renamed_tools import RENAMED_TOOLS

    contract = json.loads(
        (_CONTRACTS / "compatibility.json").read_text(encoding="utf-8")
    )
    missing = {
        old: new
        for old, new in contract["retired_tool_aliases"].items()
        if RENAMED_TOOLS.get(old) != new
    }
    assert not missing, f"Cached clients lost their retired tool aliases: {missing}"


def test_native_engine_advertises_the_existing_contract_of_each_migrated_tool() -> None:
    """Moving a tool to Assist/native MCP must preserve the actual old schema."""
    from ha_mcp.runtime.catalog import build_initial_catalog

    baseline = {
        tool["name"]: tool
        for tool in json.loads(
            (_CONTRACTS / "catalog-baseline.json").read_text(encoding="utf-8")
        )["tools"]
    }
    extras = json.loads(
        (_CONTRACTS / "catalog-extras-baseline.json").read_text(encoding="utf-8")
    )["tools"]
    baseline.update({tool["name"]: tool for tool in extras})
    overrides = json.loads(
        (_CONTRACTS / "compatibility.json").read_text(encoding="utf-8")
    )["native_annotation_overrides"]
    assert set(overrides) == {
        "ha_config_list_helpers",
        "ha_get_integration",
        "ha_config_list_dashboard_resources",
    }
    assert overrides["ha_config_list_dashboard_resources"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }
    assert overrides["ha_config_list_dashboard_resources"]["reason"]
    assert overrides["ha_get_integration"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }
    assert overrides["ha_get_integration"]["reason"]
    assert overrides["ha_config_list_helpers"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }
    assert overrides["ha_config_list_helpers"]["reason"]
    definitions = build_initial_catalog(MagicMock())
    integration_reader = next(
        tool for tool in definitions if tool.name == "ha_get_integration"
    )
    subentry_help = integration_reader.input_schema["properties"][
        "include_subentry_schema"
    ]["description"]
    assert "may create or change a subentry" in subentry_help
    assert "no subentry is created" not in subentry_help
    declared = {definition.name for definition in definitions}
    assert {tool["name"] for tool in extras} <= declared, (
        "Native entry lost an always-present server tool"
    )
    changes = {}
    for definition in definitions:
        old = baseline.get(definition.name)
        if old is None:
            continue
        differences = _changed_inputs(old["inputSchema"], definition.input_schema)
        expected_annotations = {
            **old["annotations"],
            **overrides.get(definition.name, {}).get("annotations", {}),
        }
        for hint in _ANNOTATIONS:
            if expected_annotations.get(hint) != definition.annotations.get(hint):
                differences.append(f"changed safety annotation {hint}")
        if set(old.get("_meta", {}).get("fastmcp", {}).get("tags", [])) != set(
            definition.tags
        ):
            differences.append("changed discovery categories")
        if _structural_schema(old.get("outputSchema")) != _structural_schema(
            definition.output_schema
        ):
            differences.append("changed advertised output schema")
        if differences:
            changes[definition.name] = differences
    assert not changes, f"Native tool definitions broke baseline contracts: {changes}"
