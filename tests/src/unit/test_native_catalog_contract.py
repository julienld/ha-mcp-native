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


def _shared_annotation_overrides() -> dict[str, Any]:
    return json.loads((_CONTRACTS / "compatibility.json").read_text(encoding="utf-8"))[
        "shared_annotation_overrides"
    ]


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


def _adds_error_log_cursor(before: Any, after: Any) -> bool:
    """The upstream log cursor extends exactly the existing bounded offset."""
    return _structural_schema(before) == {
        "default": 0,
        "type": "integer",
        "minimum": 0,
    } and _structural_schema(after) == {
        "default": 0,
        "anyOf": [{"type": "integer", "minimum": 0}, {"type": "string"}],
    }


def _upstream_string_extension(
    tool_name: str | None, name: str, before: Any, after: Any
) -> bool:
    """Recognize only the string-input extensions shipped by upstream 1c4b4edfc.

    The captured baseline stays immutable. These exact additions retain every
    old input; a changed default, bound or another parameter still fails.
    """
    targets = {
        ("ha_get_hacs_info", "category"): False,
        ("ha_manage_hacs", "category"): False,
        ("ha_get_todo", "status"): True,
        ("ha_set_todo_item", "status"): False,
        ("ha_remove_todo_item", "item"): True,
    }
    key = (tool_name, name)
    if key not in targets:
        return False
    old, new = _structural_schema(before), _structural_schema(after)
    if key == ("ha_remove_todo_item", "item"):
        if old != {"type": "string"}:
            return False
    else:
        branches = old.get("anyOf", [])
        if (
            set(old) != {"anyOf", "default"}
            or old["default"] is not None
            or len(branches) != 2
            or branches[1] != {"type": "null"}
            or set(branches[0]) != {"enum", "type"}
            or branches[0]["type"] != "string"
        ):
            return False
    alternatives = [{"type": "string"}]
    if targets[key]:
        alternatives.append({"items": {"type": "string"}, "type": "array"})
    alternatives.append({"type": "null"})
    return new == {"anyOf": alternatives, "default": None}


def _optional_nullable_input(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return _structural_schema(before) == {"type": "string"} and _structural_schema(
        after
    ) == {"default": None, "anyOf": [{"type": "string"}, {"type": "null"}]}


def _preserved_definitions(before: Any, after: Any) -> bool:
    old, new = before or {}, after or {}
    return (
        isinstance(old, dict)
        and isinstance(new, dict)
        and all(
            key in new and _structural_schema(value) == _structural_schema(new[key])
            for key, value in old.items()
        )
    )


def _changed_inputs(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    tool_name: str | None = None,
) -> list[str]:
    changes = []
    old_properties = before.get("properties", {})
    new_properties = after.get("properties", {})
    for name, schema in old_properties.items():
        if name not in new_properties:
            changes.append(f"removed parameter {name}")
        elif _structural_schema(schema) != _structural_schema(new_properties[name]):
            if not (
                (
                    name in before.get("required", [])
                    and name not in after.get("required", [])
                    and _optional_nullable_input(schema, new_properties[name])
                )
                or (
                    tool_name == "ha_get_logs"
                    and name == "offset"
                    and _adds_error_log_cursor(schema, new_properties[name])
                )
                or _upstream_string_extension(
                    tool_name, name, schema, new_properties[name]
                )
                or (
                    tool_name == "ha_manage_backup"
                    and name == "restore_database"
                    and _structural_schema(schema)
                    == {"default": False, "type": "boolean"}
                    and _structural_schema(new_properties[name])
                    == {
                        "default": None,
                        "anyOf": [{"type": "boolean"}, {"type": "null"}],
                    }
                )
            ):
                changes.append(f"changed parameter {name}")
    if set(after.get("required", [])) - set(before.get("required", [])):
        changes.append("newly required parameters")
    changes.extend(
        f"changed root {key}"
        for key in ("$defs", "definitions", "additionalProperties", "type")
        if _structural_schema(before.get(key)) != _structural_schema(after.get(key))
        and not (
            key in {"$defs", "definitions"}
            and _preserved_definitions(before.get(key), after.get(key))
        )
    )
    return changes


@pytest.mark.parametrize("database_included", [False, True])
async def test_public_backup_default_restores_the_database_only_when_the_archive_has_it(
    database_included: bool,
) -> None:
    from ha_mcp.runtime.backup_tools import BackupFacade
    from ha_mcp.tools.backup_snapshot_restore import restore_backup

    from ._backup_snapshot_fakes import FakeWS, _client, _entry, _run

    ws = FakeWS(details=_entry("b1", database_included=database_included))

    async def manage_backups(**arguments):
        return await _run(
            restore_backup,
            ws,
            _client(),
            arguments["backup_id"],
            restore_database=arguments["restore_database"],
        )

    await BackupFacade(manage_backups).ha_manage_backup(
        scope="snapshot", action="restore", backup_id="b1"
    )
    assert ws.sent("backup/restore")[0]["restore_database"] is database_included


@pytest.mark.parametrize("mutation", [None, "default", "type", "other_parameter"])
def test_archive_database_exception_is_limited_to_its_reviewed_schema_path(mutation):
    name = "other_parameter" if mutation == "other_parameter" else "restore_database"
    new = {"default": None, "anyOf": [{"type": "boolean"}, {"type": "null"}]}
    if mutation == "default":
        new["default"] = True
    elif mutation == "type":
        new["anyOf"][0] = {"type": "integer"}
    changes = _changed_inputs(
        {"properties": {name: {"default": False, "type": "boolean"}}},
        {"properties": {name: new}},
        tool_name="ha_manage_backup",
    )
    assert changes == ([] if mutation is None else [f"changed parameter {name}"])


@pytest.mark.parametrize(
    "mutation",
    [None, "default", "bound", "item_type", "other_tool"],
)
def test_upstream_todo_status_extension_keeps_prior_values_without_waiving_other_drift(
    mutation: str | None,
) -> None:
    old = {
        "anyOf": [
            {"enum": ["needs_action", "completed"], "type": "string"},
            {"type": "null"},
        ],
        "default": None,
    }
    new = {
        "anyOf": [
            {"type": "string"},
            {"items": {"type": "string"}, "type": "array"},
            {"type": "null"},
        ],
        "default": None,
    }
    if mutation == "default":
        new["default"] = "completed"
    elif mutation == "bound":
        new["anyOf"][0]["minLength"] = 20
    elif mutation == "item_type":
        new["anyOf"][1]["items"] = {"type": "integer"}
    changes = _changed_inputs(
        {"properties": {"status": old}},
        {"properties": {"status": new}},
        tool_name="ha_other_tool" if mutation == "other_tool" else "ha_get_todo",
    )
    assert changes == ([] if mutation is None else ["changed parameter status"])


def test_unstructured_return_does_not_advertise_a_schema_but_dict_return_keeps_one() -> (
    None
):
    from ha_mcp.runtime.catalog import define_tool

    class Outputs:
        async def unstructured(self) -> Any:
            return None

        async def object_result(self) -> dict[str, Any]:
            return {}

    unstructured, object_result = (
        define_tool(MagicMock(), Outputs, name, annotations={})
        for name in ("unstructured", "object_result")
    )
    assert unstructured.output_schema is None
    assert (
        object_result.output_schema and object_result.output_schema["type"] == "object"
    )


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
    shared = _shared_annotation_overrides()
    assert set(shared) == {"ha_remove_todo_item"}
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
        differences = _changed_inputs(
            old["inputSchema"], current["inputSchema"], tool_name=name
        )
        for hint in _ANNOTATIONS:
            expected_hint = (
                shared.get(name, {})
                .get("annotations", {})
                .get(hint, old["annotations"].get(hint))
            )
            if expected_hint != current.get("annotations", {}).get(hint):
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
    overrides = (
        _shared_annotation_overrides()
        | json.loads((_CONTRACTS / "compatibility.json").read_text(encoding="utf-8"))[
            "native_annotation_overrides"
        ]
    )
    assert set(overrides) == {
        "ha_remove_todo_item",
        "ha_config_list_helpers",
        "ha_get_integration",
        "ha_config_list_dashboard_resources",
        "ha_config_get_dashboard",
        "ha_get_dashboard_screenshot",
    }
    assert overrides["ha_config_get_dashboard"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }
    assert overrides["ha_config_get_dashboard"]["reason"]
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
        differences = _changed_inputs(
            old["inputSchema"], definition.input_schema, tool_name=definition.name
        )
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


@pytest.mark.parametrize(
    "replacement, compatible",
    [
        (
            {
                "default": 0,
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "string"}],
            },
            True,
        ),
        (
            {
                "default": 0,
                "anyOf": [{"type": "integer", "minimum": 1}, {"type": "string"}],
            },
            False,
        ),
        ({"default": 0, "anyOf": [{"type": "integer"}, {"type": "string"}]}, False),
        (
            {
                "default": 1,
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "string"}],
            },
            False,
        ),
    ],
)
def test_added_cursor_alternative_must_retain_integer_bounds_and_default(
    replacement: dict[str, Any],
    compatible: bool,
) -> None:
    baseline = {"default": 0, "type": "integer", "minimum": 0}
    before = {"properties": {"offset": baseline}}
    after = {"properties": {"offset": replacement}}
    assert (not _changed_inputs(before, after, tool_name="ha_get_logs")) is compatible
    assert _changed_inputs(before, after, tool_name="ha_other_tool") == [
        "changed parameter offset"
    ]


def test_optional_schema_extensions_keep_existing_call_restrictions():
    import copy

    before = {
        "type": "object",
        "properties": {
            "identifier": {"type": "string"},
            "choice": {"$ref": "#/$defs/Choice"},
        },
        "required": ["identifier"],
        "$defs": {"Choice": {"type": "string", "enum": ["a", "b"]}},
        "additionalProperties": False,
    }
    after = copy.deepcopy(before)
    after["properties"]["identifier"] = {
        "default": None,
        "anyOf": [{"type": "string"}, {"type": "null"}],
    }
    after["required"] = []
    after["properties"]["edits"] = {"type": "array", "items": {"$ref": "#/$defs/Edit"}}
    after["$defs"]["Edit"] = {"type": "object"}
    assert not _changed_inputs(before, after)
    for mutation in ("definition", "required", "removed", "type"):
        broken = copy.deepcopy(after)
        if mutation == "definition":
            broken["$defs"]["Choice"]["enum"] = ["a"]
        elif mutation == "required":
            broken["required"] = ["edits"]
        elif mutation == "removed":
            del broken["properties"]["identifier"]
        else:
            broken["properties"]["identifier"] = {"type": "integer"}
        assert _changed_inputs(before, broken), mutation
